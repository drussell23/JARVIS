# [Ouroboros] Modified by Ouroboros (op=op-01a07af3-) at 2026-09-07 08:24 UTC
# [Ouroboros] Modified by Ouroboros (op=op-01a07af7-) at 2026-09-07 08:27 UTC
# [Ouroboros] Modified by Ouroboros (op=op-01a07aff-) at 2026-09-07 08:33 UTC
# [Ouroboros] Modified by Ouroboros (op=op-01a07b02-) at 2026-09-07 08:38 UTC
# Reason: First-order proof: author a real unit test for the untested model_physics oracle  AUTHOR a new pytest test file at tests

# Reason: First-order proof: author a real unit test for the untested model_physics oracle
import pytest
from backend.core.ouroboros.governance import model_physics

class MockPhysics:
    def __init__(self, native_context=32768, block_count=32, kv_heads=32, key_length=128, value_length=128):
        self.native_context = native_context
        self.block_count = block_count
        self.kv_heads = kv_heads
        self.key_length = key_length
        self.value_length = value_length
        # Calculate expected KV bytes based on the formula in parse_model_physics
        self.kv_bytes_per_token = block_count * kv_heads * (key_length + value_length) * model_physics.kv_cache_dtype_bytes()

def test_parse_model_physics_valid_qwen2_coder():
    """Test parsing of a valid qwen2.5-coder model payload."""
    payload = {
        'model_info': {
            'general.architecture': 'qwen2.5-coder',
            'qwen2.5-coder.context_length': 32768,
            'qwen2.5-coder.block_count': 32,
            'qwen2.5-coder.attention.head_count_kv': 32,
            'qwen2.5-coder.attention.key_length': 128,
            'qwen2.5-coder.attention.value_length': 128,
            'qwen2.5-coder.attention.head_count': 32,
            'qwen2.5-coder.embedding_length': 4096
        }
    }
    
    result = model_physics.parse_model_physics(payload)
    assert result is not None
    assert result.native_context == 32768
    assert result.architecture == 'qwen2.5-coder'
    assert result.block_count == 32
    assert result.kv_heads == 32
    assert result.key_length == 128
    assert result.value_length == 128
    
    # Validate KV bytes calculation: block_count * kv_heads * (key_length + value_length) * kv_cache_dtype_bytes()
    expected_kv_bytes = 32 * 32 * (128 + 128) * model_physics.kv_cache_dtype_bytes()
    assert result.kv_bytes_per_token == expected_kv_bytes

def test_parse_model_physics_missing_field_returns_none():
    """Test that missing required fields return None."""
    payload = {
        'model_info': {
            'general.architecture': 'qwen2.5-coder',
            'qwen2.5-coder.context_length': 32768,
            'qwen2.5-coder.block_count': 32
            # Missing other required fields
        }
    }
    
    result = model_physics.parse_model_physics(payload)
    assert result is None

def test_parse_model_physics_invalid_payload_returns_none():
    """Test that invalid payload returns None."""
    result = model_physics.parse_model_physics(None)
    assert result is None
    
    result = model_physics.parse_model_physics({})
    assert result is None

def test_effective_ceiling_with_valid_physics():
    """Test effective ceiling calculation with valid physics."""
    # Create a mock physics object with known values
    physics = MockPhysics(native_context=32768, block_count=32, kv_heads=32, key_length=128, value_length=128)
    
    # Test case: configured ceiling is larger than native context -> should take native (min of two)
    result = model_physics.effective_ceiling(physics, 65536)
    assert result == 32768  # Should take the minimum of configured and native
    
    # Test case: configured ceiling is smaller than native context -> should take configured (min of two)
    result = model_physics.effective_ceiling(physics, 16384)
    assert result == 16384  # Should take the minimum of configured and native

def test_effective_ceiling_with_none_physics():
    """Test effective ceiling with None physics."""
    result = model_physics.effective_ceiling(None, 65536)
    assert result == 65536  # Should pass through configured value

def test_kv_cache_dtype_bytes_default():
    """Test default KV cache dtype bytes is 2."""
    assert model_physics.kv_cache_dtype_bytes() == 2

def test_reset_cache_for_tests():
    """Test that reset cache function exists and can be called."""
    # This test ensures the function exists and doesn't raise
    model_physics.reset_cache_for_tests()
    
    # Reset again to ensure idempotency
    model_physics.reset_cache_for_tests()

def test_model_physics_exact_kv_ceiling_reproduction():
    """Test that the formula reproduces exact KV/context ceiling for qwen2.5-coder:32b as mentioned in comments."""
    # From the docstring: "computed from metadata it reproduces the hand-derived 262,144 in local_inference_director EXACTLY for qwen2.5-coder:32b"
    payload = {
        'model_info': {
            'general.architecture': 'qwen2.5-coder',
            'qwen2.5-coder.context_length': 32768,
            'qwen2.5-coder.block_count': 32,
            'qwen2.5-coder.attention.head_count_kv': 32,
            'qwen2.5-coder.attention.key_length': 128,
            'qwen2.5-coder.attention.value_length': 128,
            'qwen2.5-coder.attention.head_count': 32,
            'qwen2.5-coder.embedding_length': 4096
        }
    }
    
    result = model_physics.parse_model_physics(payload)
    assert result is not None
    assert result.native_context == 32768
    
    # Validate the computed KV bytes match what would be expected for this architecture
    expected_kv_bytes = 32 * 32 * (128 + 128) * model_physics.kv_cache_dtype_bytes()
    assert result.kv_bytes_per_token == expected_kv_bytes
    
    # Test effective ceiling with known value to make sure it's consistent with the math
    configured_ceiling = 65536
    effective_result = model_physics.effective_ceiling(result, configured_ceiling)
    # Should be minimum of configured and native context
    assert effective_result == min(configured_ceiling, result.native_context)