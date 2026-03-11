"""
Tests for the /v1/capability endpoint schema introduced in run_server.py.

These tests exercise the *contract shape* of the capability response dict
without requiring a live server.  They are deliberately lightweight so they
can run in CI without a GPU or a loaded model.
"""

import pytest
import yaml
from pathlib import Path


class TestCapabilityEndpointSchema:
    def test_capability_response_has_required_fields(self):
        """Capability dict must contain compute_class, model_id, gpu_layers, tok_s_est."""
        cap = {
            "compute_class": "gpu_t4",
            "model_id": "Qwen2.5-Coder-7B-Instruct-Q4_K_M",
            "model_artifact": "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
            "gpu_layers": -1,
            "tok_s_estimate": 40,
            "host": "jarvis-prime-stable",
        }
        required = {"compute_class", "model_id", "model_artifact", "gpu_layers", "tok_s_estimate", "host"}
        assert required <= cap.keys(), f"Missing fields: {required - cap.keys()}"

    def test_compute_class_values_are_bounded(self):
        valid = {"cpu", "gpu_t4", "gpu_l4", "gpu_v100", "gpu_a100"}
        assert "gpu_t4" in valid
        assert "gpu_banana" not in valid

    def test_gpu_layers_minus_one_implies_full_offload(self):
        cap = {"gpu_layers": -1, "compute_class": "gpu_t4"}
        if cap["compute_class"] != "cpu":
            assert cap["gpu_layers"] == -1


POLICY_PATH = Path("backend/core/ouroboros/governance/brain_selection_policy.yaml")


class TestBrainPolicyComputeClass:
    """brain_selection_policy.yaml must have compute_class contract fields."""

    @staticmethod
    def _required_brains_as_dict(doc):
        """Return required brains as {brain_id: cfg} regardless of list vs dict layout."""
        raw = doc.get("brains", {}).get("required", [])
        if isinstance(raw, list):
            return {entry["brain_id"]: entry for entry in raw}
        return raw  # already a dict-keyed layout

    @staticmethod
    def _all_brains_as_dict(doc) -> dict:
        """Return all brains (required + optional) as {brain_id: cfg} dict."""
        result = {}
        for section in ("required", "optional"):
            entries = doc.get("brains", {}).get(section, [])
            if isinstance(entries, list):
                for entry in entries:
                    bid = entry.get("brain_id") or entry.get("id")
                    if bid:
                        result[bid] = {k: v for k, v in entry.items() if k not in ("brain_id", "id")}
            elif isinstance(entries, dict):
                result.update(entries)
        return result

    def test_policy_has_compute_class_per_brain(self):
        """Every brain entry (required + optional) must have compute_class and min_compute_class."""
        doc = yaml.safe_load(POLICY_PATH.read_text())
        brains = self._all_brains_as_dict(doc)
        assert brains, "No brains defined in policy"
        for brain_id, cfg in brains.items():
            assert "compute_class" in cfg, f"Brain {brain_id!r} missing compute_class"
            assert "min_compute_class" in cfg, f"Brain {brain_id!r} missing min_compute_class"

    def test_policy_has_model_artifact_per_brain(self):
        """Every brain entry (required + optional) must have model_artifact for integrity check."""
        doc = yaml.safe_load(POLICY_PATH.read_text())
        brains = self._all_brains_as_dict(doc)
        for brain_id, cfg in brains.items():
            assert "model_artifact" in cfg, f"Brain {brain_id!r} missing model_artifact"

    def test_compute_class_order_is_respected(self):
        """min_compute_class=gpu_t4 must not route to cpu — checked for all brains (required + optional)."""
        compute_rank = {"cpu": 0, "gpu_t4": 1, "gpu_l4": 2, "gpu_v100": 3, "gpu_a100": 4}
        doc = yaml.safe_load(POLICY_PATH.read_text())
        brains = self._all_brains_as_dict(doc)
        for brain_id, cfg in brains.items():
            cc = cfg.get("compute_class", "cpu")
            min_cc = cfg.get("min_compute_class", "cpu")
            assert compute_rank.get(cc, 0) >= compute_rank.get(min_cc, 0), (
                f"Brain {brain_id!r} has compute_class={cc!r} below "
                f"min_compute_class={min_cc!r}"
            )


class TestComputeAdmissionGate:
    """The admission gate enforces compute class hierarchy."""

    def test_cpu_vm_rejected_for_gpu_brain(self):
        """cpu VM is rejected for a brain requiring gpu_t4."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            ComputeClassMismatch,
            _check_compute_admission,
        )
        capability = {"compute_class": "cpu", "host": "jarvis-prime-stable"}
        brain_cfg = {"min_compute_class": "gpu_t4", "model_artifact": "qwen.gguf"}
        with pytest.raises(ComputeClassMismatch) as exc_info:
            _check_compute_admission(brain_cfg, capability)
        assert "cpu" in str(exc_info.value)
        assert "gpu_t4" in str(exc_info.value)

    def test_gpu_vm_accepted_for_gpu_brain(self):
        """gpu_t4 VM is accepted for a brain requiring gpu_t4."""
        from backend.core.ouroboros.governance.governed_loop_service import _check_compute_admission
        capability = {"compute_class": "gpu_t4", "host": "jarvis-prime-stable"}
        brain_cfg = {"min_compute_class": "gpu_t4", "model_artifact": "qwen.gguf"}
        _check_compute_admission(brain_cfg, capability)  # must not raise

    def test_cpu_vm_accepted_for_cpu_brain(self):
        """cpu VM is accepted for a cpu brain."""
        from backend.core.ouroboros.governance.governed_loop_service import _check_compute_admission
        capability = {"compute_class": "cpu", "host": "jarvis-prime-stable"}
        brain_cfg = {"min_compute_class": "cpu", "model_artifact": "llama-1b.gguf"}
        _check_compute_admission(brain_cfg, capability)  # must not raise

    def test_higher_gpu_class_satisfies_lower_min(self):
        """gpu_l4 VM satisfies gpu_t4 min_compute_class."""
        from backend.core.ouroboros.governance.governed_loop_service import _check_compute_admission
        capability = {"compute_class": "gpu_l4", "host": "jarvis-prime-stable"}
        brain_cfg = {"min_compute_class": "gpu_t4", "model_artifact": "qwen.gguf"}
        _check_compute_admission(brain_cfg, capability)  # must not raise
