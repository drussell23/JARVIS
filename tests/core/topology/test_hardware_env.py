"""Tests for HardwareEnvironmentState dynamic discovery."""
import platform
from unittest.mock import patch

import pytest

from backend.core.topology.hardware_env import (
    ComputeTier,
    GPUState,
    HardwareEnvironmentState,
)


class TestComputeTier:
    def test_enum_values(self):
        assert ComputeTier.CLOUD_GPU == "cloud_gpu"
        assert ComputeTier.CLOUD_CPU == "cloud_cpu"
        assert ComputeTier.LOCAL_GPU == "local_gpu"
        assert ComputeTier.LOCAL_CPU == "local_cpu"
        assert ComputeTier.UNKNOWN == "unknown"


class TestGPUState:
    def test_frozen(self):
        gpu = GPUState(name="L4", vram_total_mb=24576, vram_free_mb=20000, driver_version="535.0")
        with pytest.raises(AttributeError):
            gpu.name = "A100"

    def test_fields(self):
        gpu = GPUState(name="L4", vram_total_mb=24576, vram_free_mb=20000, driver_version="535.0")
        assert gpu.name == "L4"
        assert gpu.vram_total_mb == 24576


class TestHardwareEnvironmentState:
    def test_frozen(self):
        state = HardwareEnvironmentState(
            os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
            ram_available_mb=8192, compute_tier=ComputeTier.LOCAL_CPU, gpu=None,
            hostname="test", python_version="3.11.0",
            max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
        )
        with pytest.raises(AttributeError):
            state.os_family = "linux"

    def test_discover_returns_valid_state(self):
        state = HardwareEnvironmentState.discover()
        assert state.os_family == platform.system().lower()
        assert state.cpu_logical_cores >= 1
        assert state.ram_total_mb > 0
        assert state.ram_available_mb > 0
        assert state.max_parallel_inference_tasks >= 1
        assert state.max_shadow_harness_workers >= 1
        assert isinstance(state.compute_tier, ComputeTier)

    def test_discover_no_hardcoded_tier(self):
        state = HardwareEnvironmentState.discover()
        if platform.system().lower() == "darwin":
            assert state.gpu is None
            assert state.compute_tier in (ComputeTier.LOCAL_CPU, ComputeTier.LOCAL_GPU)

    def test_classify_tier_cloud_gpu(self):
        gpu = GPUState(name="L4", vram_total_mb=24576, vram_free_mb=20000, driver_version="535.0")
        with patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT": "jarvis-473803"}):
            tier = HardwareEnvironmentState._classify_tier(gpu, 4, 16384)
        assert tier == ComputeTier.CLOUD_GPU

    def test_classify_tier_cloud_cpu(self):
        with patch.dict("os.environ", {"AWS_REGION": "us-east-1"}):
            tier = HardwareEnvironmentState._classify_tier(None, 4, 16384)
        assert tier == ComputeTier.CLOUD_CPU

    def test_classify_tier_local_gpu(self):
        gpu = GPUState(name="RTX4090", vram_total_mb=24576, vram_free_mb=20000, driver_version="535.0")
        with patch.dict("os.environ", {}, clear=True):
            tier = HardwareEnvironmentState._classify_tier(gpu, 8, 32768)
        assert tier == ComputeTier.LOCAL_GPU

    def test_classify_tier_local_cpu(self):
        with patch.dict("os.environ", {}, clear=True):
            tier = HardwareEnvironmentState._classify_tier(None, 8, 16384)
        assert tier == ComputeTier.LOCAL_CPU

    def test_probe_gpu_returns_none_without_nvidia(self):
        result = HardwareEnvironmentState._probe_gpu()
        if platform.system().lower() == "darwin":
            assert result is None

    def test_max_parallel_inference_derived(self):
        state = HardwareEnvironmentState.discover()
        expected = max(1, state.ram_available_mb // 2048)
        assert state.max_parallel_inference_tasks == expected

    def test_max_shadow_workers_derived(self):
        state = HardwareEnvironmentState.discover()
        expected = max(1, state.cpu_logical_cores // 2)
        assert state.max_shadow_harness_workers == expected
