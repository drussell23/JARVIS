"""Task 3 — cryptographic-nonce branch naming (collision-proof AutoCommitter)."""
from __future__ import annotations


def test_compute_workspace_nonce_is_deterministic_6char_hex():
    from backend.core.ouroboros.governance.autonomous_workspace import (
        compute_workspace_nonce,
    )

    n = compute_workspace_nonce("bt-X", "123.45")
    assert len(n) == 6
    assert all(c in "0123456789abcdef" for c in n)
    # Deterministic in (session_id, salt).
    assert compute_workspace_nonce("bt-X", "123.45") == n
    # Salt varies -> different nonce (cross-boot collision-proof).
    assert compute_workspace_nonce("bt-X", "999.9") != n
    # Session varies -> different nonce.
    assert compute_workspace_nonce("bt-Y", "123.45") != n


def test_workspace_branch_is_nonced_and_stable_per_session():
    from backend.core.ouroboros.governance.autonomous_workspace import workspace_branch

    b1 = workspace_branch("bt-Z")
    b2 = workspace_branch("bt-Z")
    # Stable within a boot -> file + commit isolation still converge on ONE branch.
    assert b1 == b2
    assert b1.startswith("ouroboros/auto/bt-Z-")
    # 6-char nonce suffix.
    assert len(b1.rsplit("-", 1)[1]) == 6
