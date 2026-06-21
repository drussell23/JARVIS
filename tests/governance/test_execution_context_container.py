from __future__ import annotations
from backend.core.ouroboros.governance import execution_context as ec

def test_not_container_by_default(monkeypatch, tmp_path):
    for k in ("OUROBOROS_CLOUD_NODE", "KUBERNETES_SERVICE_HOST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ec, "_DOCKERENV_PATH", str(tmp_path / "nope"), raising=False)
    assert ec._is_cloud_container() is False

def test_container_via_env_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("OUROBOROS_CLOUD_NODE", "1")
    assert ec._is_cloud_container() is True

def test_container_via_k8s_marker(monkeypatch, tmp_path):
    monkeypatch.delenv("OUROBOROS_CLOUD_NODE", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert ec._is_cloud_container() is True

def test_container_via_dockerenv(monkeypatch, tmp_path):
    monkeypatch.delenv("OUROBOROS_CLOUD_NODE", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    d = tmp_path / ".dockerenv"; d.write_text("", encoding="utf-8")
    monkeypatch.setattr(ec, "_DOCKERENV_PATH", str(d), raising=False)
    assert ec._is_cloud_container() is True
