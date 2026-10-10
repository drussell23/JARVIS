# [Ouroboros] Modified by Ouroboros (op=op-01a1230b-) at 2026-10-09 23:43 UTC
# Reason: `backend/contracts/contract_version.py` has no corresponding test module. CREATE `tests/test_contract_version.py` contai

from __future__ import annotations
from unittest.mock import patch, MagicMock
from backend.contracts.contract_version import (
    ContractVersion,
    compute_policy_hash,
    LOCAL_CONTRACT,
)

def test_contract_version_import_smoke_test():
    # Ensure the module can be imported without errors
    assert ContractVersion is not None
    assert compute_policy_hash is not None
    assert LOCAL_CONTRACT is not None


def test_contract_version_is_compatible():
    contract = ContractVersion(
        current=(0, 3, 0),
        min_supported=(0, 2, 0),
        max_supported=(0, 4, 0),
    )
    
    # Test compatible version
    result = contract.is_compatible((0, 3, 0))
    assert result == (True, "compatible")
    
    # Test version below min_supported
    result = contract.is_compatible((0, 1, 0))
    assert result[0] is False
    assert "below min_supported" in result[1]
    
    # Test version above max_supported
    result = contract.is_compatible((0, 5, 0))
    assert result[0] is False
    assert "above max_supported" in result[1]


def test_contract_version_to_dict():
    contract = ContractVersion(
        current=(0, 3, 0),
        min_supported=(0, 2, 0),
        max_supported=(0, 4, 0),
    )
    
    expected = {
        "current": [0, 3, 0],
        "min_supported": [0, 2, 0],
        "max_supported": [0, 4, 0],
    }
    
    assert contract.to_dict() == expected


def test_compute_policy_hash():
    # Test basic functionality
    policy_data = {"key": "value", "number": 42}
    hash1 = compute_policy_hash(policy_data)
    
    # Test that same data produces same hash
    hash2 = compute_policy_hash(policy_data)
    assert hash1 == hash2
    
    # Test that different data produces different hash
    policy_data2 = {"key": "value", "number": 43}
    hash3 = compute_policy_hash(policy_data2)
    assert hash1 != hash3
    
    # Test with nested structures
    nested_data = {"a": [1, 2, 3], "b": {"c": "d"}}
    hash4 = compute_policy_hash(nested_data)
    assert isinstance(hash4, str)
    assert len(hash4) == 16


def test_local_contract():
    # Test that the local contract has expected values
    assert LOCAL_CONTRACT.current == (0, 3, 0)
    assert LOCAL_CONTRACT.min_supported == (0, 2, 0)
    assert LOCAL_CONTRACT.max_supported == (0, 4, 0)
    
    # Test that local contract is compatible with its own version
    result = LOCAL_CONTRACT.is_compatible((0, 3, 0))
    assert result == (True, "compatible")
    
    # Test edge cases for local contract
    result = LOCAL_CONTRACT.is_compatible((0, 2, 0))  # min supported
    assert result == (True, "compatible")
    
    result = LOCAL_CONTRACT.is_compatible((0, 4, 0))  # max supported
    assert result == (True, "compatible")