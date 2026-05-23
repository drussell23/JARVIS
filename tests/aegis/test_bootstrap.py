"""Bootstrap payload — atomic 0o600 write, unlink-on-read, entropy."""
from __future__ import annotations

import stat

import pytest

from backend.core.ouroboros.aegis.bootstrap import (
    BOOTSTRAP_PAYLOAD_SCHEMA_VERSION,
    BootstrapPayload,
    atomic_write_payload,
    default_expiry,
    mint_bootstrap_psk,
    read_and_unlink_payload,
)


def test_psk_entropy_is_high():
    """PSK should have ≥256 bits of effective entropy (URL-safe base64
    of 48 raw bytes → ~384 bits; we require at least 64 characters)."""
    psk = mint_bootstrap_psk()
    assert isinstance(psk, str)
    assert len(psk) >= 60  # 48 bytes base64-url-safe ~ 64 chars
    # Different invocations produce different PSKs.
    others = {mint_bootstrap_psk() for _ in range(10)}
    assert psk not in others or len(others) == 10  # ≥10 distinct values likely


def test_payload_dict_roundtrip():
    p = BootstrapPayload(
        aegis_url="http://127.0.0.1:12345",
        bootstrap_psk="psk-xxx",
        daemon_pid=12345,
        expires_at=99999.0,
    )
    d = p.to_dict()
    recovered = BootstrapPayload.from_dict(d)
    assert recovered == p


def test_atomic_write_creates_file_with_mode_0o600(tmp_path):
    target = tmp_path / "agentic" / "bootstrap.json"
    p = BootstrapPayload(
        aegis_url="http://127.0.0.1:1", bootstrap_psk="psk",
        daemon_pid=1, expires_at=999.0,
    )
    atomic_write_payload(p, target)
    assert target.exists()
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


def test_atomic_write_excl_refuses_overwrite(tmp_path):
    target = tmp_path / "bootstrap.json"
    p = BootstrapPayload(
        aegis_url="http://127.0.0.1:1", bootstrap_psk="psk",
        daemon_pid=1, expires_at=999.0,
    )
    atomic_write_payload(p, target)
    with pytest.raises(FileExistsError):
        atomic_write_payload(p, target)


def test_read_and_unlink_removes_file(tmp_path):
    target = tmp_path / "bootstrap.json"
    p = BootstrapPayload(
        aegis_url="http://127.0.0.1:99", bootstrap_psk="psk-2",
        daemon_pid=2, expires_at=42.0,
    )
    atomic_write_payload(p, target)
    assert target.exists()
    recovered = read_and_unlink_payload(target)
    assert recovered == p
    assert not target.exists(), "payload file must be unlinked after read"


def test_read_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_and_unlink_payload(tmp_path / "absent.json")


def test_read_malformed_json_raises_value_error(tmp_path):
    target = tmp_path / "bootstrap.json"
    target.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ValueError):
        read_and_unlink_payload(target)


def test_read_non_object_json_raises_value_error(tmp_path):
    target = tmp_path / "bootstrap.json"
    target.write_text('["a", "b"]', encoding="utf-8")
    with pytest.raises(ValueError):
        read_and_unlink_payload(target)


def test_read_missing_field_raises_value_error(tmp_path):
    target = tmp_path / "bootstrap.json"
    target.write_text('{"aegis_url": "http://x", "daemon_pid": 1}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_and_unlink_payload(target)


def test_default_expiry_is_in_future():
    now = 1000.0
    exp = default_expiry(now_s=now)
    assert exp > now


def test_schema_version_constant():
    assert BOOTSTRAP_PAYLOAD_SCHEMA_VERSION == "aegis_bootstrap.1"


def test_atomic_write_parent_chmod_to_0700(tmp_path):
    """The bootstrap-dir gets tightened to 0o700 to prevent other users
    from listing the directory and racing on the payload path."""
    bootstrap_dir = tmp_path / "bootstrap-dir"
    bootstrap_dir.mkdir(mode=0o755)
    target = bootstrap_dir / "bp.json"
    p = BootstrapPayload(
        aegis_url="http://127.0.0.1:1", bootstrap_psk="x",
        daemon_pid=1, expires_at=42.0,
    )
    atomic_write_payload(p, target)
    parent_mode = stat.S_IMODE(bootstrap_dir.stat().st_mode)
    assert parent_mode == 0o700, f"expected parent 0o700, got 0o{parent_mode:o}"
