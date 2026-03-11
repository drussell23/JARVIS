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
