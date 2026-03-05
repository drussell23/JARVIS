"""Test that models defined in hybrid_config.yaml are loaded dynamically."""
import pytest
from pathlib import Path


class TestDynamicModelLoading:
    def test_config_models_section_exists(self):
        """hybrid_config.yaml must have jprime_llava in models section."""
        import yaml
        config_path = Path(__file__).parent.parent.parent.parent / "backend" / "core" / "hybrid_config.yaml"
        if not config_path.exists():
            pytest.skip(f"Config not found at {config_path}")
        with open(config_path) as f:
            config = yaml.safe_load(f)
        gcp = config.get("hybrid", {}).get("backends", {}).get("gcp", {})
        models = gcp.get("models", {})
        assert "jprime_llava" in models, (
            f"jprime_llava not in config models. Found: {list(models.keys())}"
        )

    def test_jprime_llava_config_has_vision(self):
        """jprime_llava config must list 'vision' capability."""
        import yaml
        config_path = Path(__file__).parent.parent.parent.parent / "backend" / "core" / "hybrid_config.yaml"
        if not config_path.exists():
            pytest.skip(f"Config not found at {config_path}")
        with open(config_path) as f:
            config = yaml.safe_load(f)
        gcp = config.get("hybrid", {}).get("backends", {}).get("gcp", {})
        models = gcp.get("models", {})
        if "jprime_llava" not in models:
            pytest.skip("jprime_llava not in config")
        llava_config = models["jprime_llava"]
        capabilities = llava_config.get("capabilities", {})
        # capabilities may be nested under 'required' key
        if isinstance(capabilities, dict):
            required = capabilities.get("required", [])
            optional = capabilities.get("optional", [])
            all_caps = required + optional
        else:
            all_caps = capabilities
        assert "vision" in all_caps, (
            f"jprime_llava capabilities: {capabilities}"
        )
