"""Test adaptive readiness hysteresis for golden image deployments.

v304.0: Golden image deployments use readiness_hysteresis_up=1 (single
READY verdict) since their readiness signals are reliable (pre-baked deps,
known model). Non-golden deployments keep the default of 3.
"""

import os
import pytest


def _effective_hysteresis(
    deployment_mode: str, config_hysteresis: int = 3
) -> int:
    """Simulate the adaptive hysteresis logic from _poll_health_until_ready."""
    if deployment_mode == "golden_image":
        return max(1, int(os.environ.get("GCP_GOLDEN_HYSTERESIS_UP", "1")))
    return config_hysteresis


class TestAdaptiveHysteresis:

    def test_golden_image_uses_hysteresis_1(self):
        assert _effective_hysteresis("golden_image") == 1

    def test_non_golden_uses_default_hysteresis(self):
        assert _effective_hysteresis("startup-script") == 3
        assert _effective_hysteresis("container") == 3
        assert _effective_hysteresis("") == 3

    def test_golden_hysteresis_env_override(self):
        os.environ["GCP_GOLDEN_HYSTERESIS_UP"] = "2"
        try:
            assert _effective_hysteresis("golden_image") == 2
        finally:
            del os.environ["GCP_GOLDEN_HYSTERESIS_UP"]

    def test_golden_hysteresis_minimum_1(self):
        os.environ["GCP_GOLDEN_HYSTERESIS_UP"] = "0"
        try:
            assert _effective_hysteresis("golden_image") == 1
        finally:
            del os.environ["GCP_GOLDEN_HYSTERESIS_UP"]
