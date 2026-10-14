"""A host that declares no Cloud SQL uses SQLite by design -- one answer, said once.

2026-10-14 boot: "[CloudSQL] Config not found, falling back to SQLite" (WARNING)
plus two more warnings for the same absent file, each component having
searched for it its own way (one of them in a single place only).
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def no_config_host(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jarvis_home"))
    monkeypatch.chdir(tmp_path)
    return home


def test_no_declared_database_is_none(no_config_host):
    from backend.intelligence.cloud_sql_connection_manager import database_config_path
    assert database_config_path() is None


def test_the_declared_database_is_found_where_any_reader_looks(no_config_host, monkeypatch, tmp_path):
    from backend.intelligence.cloud_sql_connection_manager import database_config_path
    declared = tmp_path / "jarvis_home" / "gcp" / "database_config.json"
    declared.parent.mkdir(parents=True)
    declared.write_text("{}")
    assert database_config_path() == declared


def test_the_proxy_manager_asks_the_same_question(no_config_host):
    from backend.intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager
    manager = CloudSQLProxyManager.__new__(CloudSQLProxyManager)
    with pytest.raises(FileNotFoundError, match="not configured on this host"):
        manager._discover_config_path()


@pytest.mark.parametrize("doc", [
    {"cloud_sql": {"connection_name": "proj:region:inst"}},   # the shape every reader writes
    {"connection_name": "proj:region:inst"},                  # the older top-level shape
])
def test_the_lifecycle_controller_reads_the_connection_name(no_config_host, monkeypatch, doc):
    from backend.core.proxy.lifecycle_controller import ProxyConfig as C
    path = no_config_host / ".jarvis" / "gcp" / "database_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(doc))
    monkeypatch.setattr(C, "GCP_PROJECT", ""); monkeypatch.setattr(C, "GCP_INSTANCE", "")
    assert C.get_connection_string() == "proj:region:inst"
