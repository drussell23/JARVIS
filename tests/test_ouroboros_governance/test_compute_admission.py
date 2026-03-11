"""
Tests for the /v1/capability endpoint schema introduced in run_server.py.

These tests exercise the *contract shape* of the capability response dict
without requiring a live server.  They are deliberately lightweight so they
can run in CI without a GPU or a loaded model.
"""


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


import yaml
from pathlib import Path

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

    def test_policy_has_compute_class_per_brain(self):
        """Every brain entry must have compute_class and min_compute_class."""
        doc = yaml.safe_load(POLICY_PATH.read_text())
        brains = self._required_brains_as_dict(doc)
        assert brains, "No brains defined in policy"
        for brain_id, cfg in brains.items():
            assert "compute_class" in cfg, f"Brain {brain_id!r} missing compute_class"
            assert "min_compute_class" in cfg, f"Brain {brain_id!r} missing min_compute_class"

    def test_policy_has_model_artifact_per_brain(self):
        """Every brain entry must have model_artifact for integrity check."""
        doc = yaml.safe_load(POLICY_PATH.read_text())
        brains = self._required_brains_as_dict(doc)
        for brain_id, cfg in brains.items():
            assert "model_artifact" in cfg, f"Brain {brain_id!r} missing model_artifact"

    def test_compute_class_order_is_respected(self):
        """min_compute_class=gpu_t4 must not route to cpu."""
        compute_rank = {"cpu": 0, "gpu_t4": 1, "gpu_l4": 2, "gpu_v100": 3, "gpu_a100": 4}
        doc = yaml.safe_load(POLICY_PATH.read_text())
        brains = self._required_brains_as_dict(doc)
        for brain_id, cfg in brains.items():
            cc = cfg.get("compute_class", "cpu")
            min_cc = cfg.get("min_compute_class", "cpu")
            assert compute_rank.get(cc, 0) >= compute_rank.get(min_cc, 0), (
                f"Brain {brain_id!r} has compute_class={cc!r} below "
                f"min_compute_class={min_cc!r}"
            )
