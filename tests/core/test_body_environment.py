"""The Body's venv follows its requirements file, not just its first install.

2026-10-15: playwright was in backend/requirements.txt but not the Windows
file, and the venv was installed from the Windows file once, at creation --
so the visual browser agent was "not set up" and the checklist could only
say "install the packages named above".
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from backend.core import body_environment as env


@pytest.fixture
def reqs(tmp_path, monkeypatch):
    """A requirements file with an include, as this platform's."""
    (tmp_path / "sub").mkdir()
    included = tmp_path / "sub" / "base.txt"
    included.write_text("fastapi\n", encoding="utf-8")
    main = tmp_path / "requirements-test.txt"
    main.write_text("-r sub/base.txt\n\nplaywright>=1.40.0\n# post-install: playwright install chromium\n"
                    "Google_Auth==2.43.0  # pinned\n", encoding="utf-8")
    monkeypatch.setattr(env, "_REQUIREMENTS", {"testos": main})
    venv = tmp_path / "venv" / "Scripts"
    venv.mkdir(parents=True)
    python = venv / "python.exe"
    python.write_text("", encoding="utf-8")
    monkeypatch.delenv("JARVIS_BODY_ENV_SYNC", raising=False)
    return types.SimpleNamespace(main=main, included=included, python=python)


def test_the_fingerprint_covers_every_included_file(reqs):
    before = env.fingerprint(reqs.main)
    reqs.included.write_text("fastapi\nuvicorn\n", encoding="utf-8")
    assert env.fingerprint(reqs.main) != before


def test_post_install_steps_are_read_from_the_file(reqs):
    assert env.post_install_steps(reqs.main) == ["playwright install chromium"]


def test_a_package_is_declared_by_its_normalized_name(reqs):
    assert env.declared("playwright", "testos")
    assert env.declared("google-auth", "testos") and env.declared("fastapi", "testos")
    assert not env.declared("selenium", "testos")
    assert not env.declared("playwright", "otheros")      # a platform not synced here


def test_a_changed_file_is_installed_then_its_steps_run_then_recorded(reqs, monkeypatch):
    ran = []
    monkeypatch.setattr(env, "_uv", lambda: "uv")
    monkeypatch.setattr(env.subprocess, "run",
                        lambda argv: ran.append(argv) or types.SimpleNamespace(returncode=0))
    assert env.sync(reqs.python, platform="testos", say=lambda _m: None)
    assert ran[0][:4] == ["uv", "pip", "install", "--python"] and ran[0][-1] == str(reqs.main)
    assert ran[1] == [str(reqs.python), "-m", "playwright", "install", "chromium"]
    assert env.in_step(reqs.python, "testos")
    ran.clear()
    assert env.sync(reqs.python, platform="testos", say=lambda _m: None)
    assert ran == []                                      # in step: nothing to do


def test_a_failed_install_is_retried_next_launch(reqs, monkeypatch):
    monkeypatch.setattr(env, "_uv", lambda: "uv")
    monkeypatch.setattr(env.subprocess, "run", lambda argv: types.SimpleNamespace(returncode=1))
    said = []
    assert env.sync(reqs.python, platform="testos", say=said.append) is False
    assert not env.in_step(reqs.python, "testos")         # no fingerprint recorded
    assert any("failed" in line for line in said)


def test_a_failed_post_install_step_is_not_recorded_either(reqs, monkeypatch):
    monkeypatch.setattr(env, "_uv", lambda: "uv")
    monkeypatch.setattr(env.subprocess, "run",
                        lambda argv: types.SimpleNamespace(returncode=0 if argv[0] == "uv" else 1))
    assert env.sync(reqs.python, platform="testos", say=lambda _m: None) is False
    assert not env.in_step(reqs.python, "testos")


def test_without_uv_it_says_so_and_changes_nothing(reqs, monkeypatch):
    monkeypatch.setattr(env, "_uv", lambda: None)
    said = []
    assert env.sync(reqs.python, platform="testos", say=said.append) is False
    assert "uv was not found" in said[0]


def test_the_operator_can_switch_it_off_and_other_platforms_are_untouched(reqs, monkeypatch):
    monkeypatch.setattr(env.subprocess, "run", lambda argv: pytest.fail("must not install"))
    monkeypatch.setenv("JARVIS_BODY_ENV_SYNC", "false")
    assert env.sync(reqs.python, platform="testos")
    monkeypatch.delenv("JARVIS_BODY_ENV_SYNC")
    assert env.sync(reqs.python, platform="otheros")


def test_the_windows_file_declares_the_visual_browser_agents_engine():
    windows = env.requirements_file("win32")
    assert env.declared("playwright", "win32")
    assert "playwright install chromium" in env.post_install_steps(windows)


def test_the_checklist_names_the_fix(monkeypatch):
    from backend.core import flight_check as fc
    binding = types.SimpleNamespace(platform_reason=lambda: "",
                                    missing_requirements=lambda: ["playwright"])
    windows = env.requirements_file("win32")
    monkeypatch.setattr(env, "requirements_file", lambda platform=None: windows)
    monkeypatch.setattr(env, "declared", lambda package, platform=None: package == "playwright")
    assert "restart JARVIS" in fc._setup_remedy({"web": binding})
    unknown = types.SimpleNamespace(platform_reason=lambda: "",
                                    missing_requirements=lambda: ["selenium"])
    assert fc._setup_remedy({"x": unknown}).startswith("add selenium to backend/requirements-")
