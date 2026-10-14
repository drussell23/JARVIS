"""JARVIS names the operator one way, by the best evidence there is."""
from __future__ import annotations

import pytest

from backend.core import host_platform, owner


@pytest.fixture(autouse=True)
def _no_declarations(monkeypatch):
    for var in ("JARVIS_OWNER_NAME", "JARVIS_USER_NAME", "JARVIS_OWNER_ADDRESS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(host_platform, "_account_full_name", "")


def test_the_declaration_wins(monkeypatch):
    monkeypatch.setattr(host_platform, "_account_full_name", "Someone Else")
    monkeypatch.setenv("JARVIS_OWNER_NAME", "Derek J. Russell")
    assert owner.owner_name() == "Derek"
    assert owner.owner_full_name() == "Derek J. Russell"


def test_the_legacy_variable_is_an_alias(monkeypatch):
    monkeypatch.setenv("JARVIS_USER_NAME", "Ada")
    assert owner.owner_name() == "Ada"


def test_then_the_os_accounts_full_name(monkeypatch):
    monkeypatch.setattr(host_platform, "_account_full_name", "Grace Hopper")
    assert owner.owner_name() == "Grace"


def test_never_the_login_name_and_never_there(monkeypatch):
    monkeypatch.setenv("USERNAME", "Jarvis")
    monkeypatch.setenv("USER", "Jarvis")
    assert owner.owner_name() == "Sir"


def test_the_form_of_address_is_declared(monkeypatch):
    monkeypatch.setenv("JARVIS_OWNER_ADDRESS", "Ma'am")
    assert owner.owner_name() == "Ma'am"
