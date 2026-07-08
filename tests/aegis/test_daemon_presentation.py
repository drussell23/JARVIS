"""Aegis subprocess silence -- ov cockpit silence (Slice 2 Task 2).

The Aegis daemon is a SUBPROCESS spawned by ``aegis/preflight.py`` with
stdout/stderr inherited from the operator's terminal. Its
``logging.basicConfig`` (``daemon.py::main``) writes INFO to that
inherited terminal -- per-request access logs (AegisPassthrough /
AegisForward), credential env-load lines, etc. -- flooding a COCKPIT
run. This suite pins the fix at the SETUP-FUNCTION level; it never
spawns a real daemon subprocess:

  §A  Presentation-mode survival across the preflight env scrub:
      ``JARVIS_OV_PRESENTATION`` is not an upstream credential (never
      touched by ``scrub_upstream_credentials``) and ``_spawn_daemon``
      carries it into the child's env via full ``os.environ``
      inheritance -- pin both halves directly (no real spawn).
  §B  ``daemon._configure_daemon_logging`` resolves ERROR-only console
      + a durable file sink under COCKPIT, and stays byte-identical
      (``logging.basicConfig(INFO, ...)``) under SOAK / no env set.
  §C  ERROR/CRITICAL always reach the console in COCKPIT (Mandate 1).
"""
from __future__ import annotations

import logging
import os
import pathlib

import pytest

from backend.core.ouroboros.aegis import daemon as daemon_mod
from backend.core.ouroboros.aegis import env_scrub
from backend.core.ouroboros.aegis import preflight as preflight_mod
from backend.core.ouroboros.aegis.credential_registry import (
    upstream_credential_env_vars,
)
from backend.core.ouroboros.ui.presentation_mode import PresentationMode


# ---------------------------------------------------------------------------
# §A -- presentation-mode survival across the env scrub / subprocess spawn
# ---------------------------------------------------------------------------


class TestPresentationModeSurvivesScrub:
    def test_presentation_mode_var_not_a_credential(self):
        """Guards against a future credential_registry edit accidentally
        classifying the presentation-mode var as an upstream credential
        (which would make the scrub strip it from the harness env)."""
        assert "JARVIS_OV_PRESENTATION" not in upstream_credential_env_vars()

    def test_scrub_upstream_credentials_leaves_presentation_mode_untouched(self):
        env = {
            "JARVIS_OV_PRESENTATION": "cockpit",
            "ANTHROPIC_API_KEY": "secret-should-be-popped",
        }
        captured = env_scrub.scrub_upstream_credentials(env)
        assert captured == {"ANTHROPIC_API_KEY": "secret-should-be-popped"}
        assert "ANTHROPIC_API_KEY" not in env
        assert env.get("JARVIS_OV_PRESENTATION") == "cockpit"

    def test_assert_no_upstream_credentials_ignores_presentation_mode(self):
        # A non-credential var present alongside a clean credential set
        # must never trip the hard post-scrub invariant.
        env = {"JARVIS_OV_PRESENTATION": "cockpit"}
        env_scrub.assert_no_upstream_credentials(env)  # must not raise


class _FakeProc:
    pid = 987654


class TestSpawnDaemonCarriesPresentationMode:
    """``_spawn_daemon`` builds the child env from a full
    ``dict(os.environ)`` snapshot BEFORE the harness-side credential
    scrub runs (see aegis_preflight's step ordering) -- so the
    presentation-mode var reaches the daemon subprocess with zero
    allowlist/payload plumbing. Pinned via a monkeypatched
    ``subprocess.Popen`` -- no real process is spawned."""

    def test_sub_env_includes_presentation_mode_when_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    ):
        monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
        captured: dict = {}

        def _fake_popen(cmd, *, env, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = env
            return _FakeProc()

        monkeypatch.setattr(preflight_mod.subprocess, "Popen", _fake_popen)
        preflight_mod._spawn_daemon(
            bootstrap_out=tmp_path / "bootstrap.json", credentials={},
        )
        assert captured["env"].get("JARVIS_OV_PRESENTATION") == "cockpit"

    def test_sub_env_absent_when_unset_in_parent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    ):
        monkeypatch.delenv("JARVIS_OV_PRESENTATION", raising=False)
        captured: dict = {}

        def _fake_popen(cmd, *, env, **kwargs):
            captured["env"] = env
            return _FakeProc()

        monkeypatch.setattr(preflight_mod.subprocess, "Popen", _fake_popen)
        preflight_mod._spawn_daemon(
            bootstrap_out=tmp_path / "bootstrap.json", credentials={},
        )
        assert "JARVIS_OV_PRESENTATION" not in captured["env"]

    def test_sub_env_untouched_by_credential_injection(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    ):
        """Credentials handed to the spawn (which the parent is about to
        scrub from its own env) never collide with / shadow the
        presentation-mode var."""
        monkeypatch.setenv("JARVIS_OV_PRESENTATION", "soak")
        captured: dict = {}

        def _fake_popen(cmd, *, env, **kwargs):
            captured["env"] = env
            return _FakeProc()

        monkeypatch.setattr(preflight_mod.subprocess, "Popen", _fake_popen)
        preflight_mod._spawn_daemon(
            bootstrap_out=tmp_path / "bootstrap.json",
            credentials={"ANTHROPIC_API_KEY": "test-key"},
        )
        assert captured["env"].get("JARVIS_OV_PRESENTATION") == "soak"
        assert captured["env"].get("ANTHROPIC_API_KEY") == "test-key"


# ---------------------------------------------------------------------------
# §B/§C -- daemon._configure_daemon_logging setup function
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_root_logger():
    """Snapshot + restore root logger state around every test in this
    module -- ``_configure_daemon_logging`` mutates process-global
    logging state (same class of concern as silent_boot's tests)."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def _console_handlers(root: logging.Logger) -> list:
    return [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]


def _file_handlers(root: logging.Logger) -> list:
    return [h for h in root.handlers if isinstance(h, logging.FileHandler)]


class TestConfigureDaemonLoggingSoak:
    def test_soak_is_byte_identical_basic_config(self):
        logging.getLogger().handlers = []
        daemon_mod._configure_daemon_logging(PresentationMode.SOAK)
        root = logging.getLogger()
        assert root.level == logging.INFO
        handlers = _console_handlers(root)
        assert len(handlers) == 1
        # basicConfig never sets an explicit handler level (NOTSET==0) --
        # this pins the exact pre-Slice-2 shape, not just "some handler".
        assert handlers[0].level == logging.NOTSET
        assert not _file_handlers(root)

    def test_default_mode_no_env_matches_soak(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("JARVIS_OV_PRESENTATION", raising=False)
        logging.getLogger().handlers = []
        daemon_mod._configure_daemon_logging()
        root = logging.getLogger()
        assert root.level == logging.INFO
        assert len(_console_handlers(root)) == 1
        assert not _file_handlers(root)

    def test_soak_info_reaches_console(self, capsys):
        logging.getLogger().handlers = []
        daemon_mod._configure_daemon_logging(PresentationMode.SOAK)
        test_logger = logging.getLogger("test.aegis.daemon.soak_info")
        test_logger.info("soak_info_should_reach_console")
        captured = capsys.readouterr()
        assert "soak_info_should_reach_console" in captured.err


class TestConfigureDaemonLoggingCockpit:
    def test_cockpit_console_handler_is_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    ):
        monkeypatch.setenv(
            "JARVIS_AEGIS_DAEMON_LOG_PATH", str(tmp_path / "daemon.log"),
        )
        logging.getLogger().handlers = []
        daemon_mod._configure_daemon_logging(PresentationMode.COCKPIT)
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        handlers = _console_handlers(root)
        assert len(handlers) == 1
        assert handlers[0].level == logging.ERROR

    def test_cockpit_installs_file_handler_at_info(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    ):
        log_path = tmp_path / "daemon.log"
        monkeypatch.setenv("JARVIS_AEGIS_DAEMON_LOG_PATH", str(log_path))
        logging.getLogger().handlers = []
        daemon_mod._configure_daemon_logging(PresentationMode.COCKPIT)
        root = logging.getLogger()
        files = _file_handlers(root)
        assert len(files) == 1
        assert files[0].level == logging.INFO
        # Compare via os.path.abspath (what FileHandler itself uses) --
        # NOT Path.resolve(), which also resolves symlinks and can
        # diverge from tmp_path on platforms where /tmp is a symlink.
        assert files[0].baseFilename == os.path.abspath(str(log_path))

    def test_cockpit_info_reaches_file_not_console(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys,
    ):
        log_path = tmp_path / "daemon.log"
        monkeypatch.setenv("JARVIS_AEGIS_DAEMON_LOG_PATH", str(log_path))
        logging.getLogger().handlers = []
        daemon_mod._configure_daemon_logging(PresentationMode.COCKPIT)

        test_logger = logging.getLogger("test.aegis.daemon.cockpit_info")
        test_logger.info("cockpit_info_should_land_in_file_only")
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:  # noqa: BLE001 — best-effort in test
                pass

        captured = capsys.readouterr()
        assert "cockpit_info_should_land_in_file_only" not in captured.err
        assert log_path.exists()
        assert "cockpit_info_should_land_in_file_only" in log_path.read_text(
            encoding="utf-8",
        )

    def test_cockpit_error_reaches_console_and_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys,
    ):
        log_path = tmp_path / "daemon.log"
        monkeypatch.setenv("JARVIS_AEGIS_DAEMON_LOG_PATH", str(log_path))
        logging.getLogger().handlers = []
        daemon_mod._configure_daemon_logging(PresentationMode.COCKPIT)

        test_logger = logging.getLogger("test.aegis.daemon.cockpit_error")
        test_logger.error("cockpit_error_should_reach_console_and_file")
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:  # noqa: BLE001 — best-effort in test
                pass

        captured = capsys.readouterr()
        assert "cockpit_error_should_reach_console_and_file" in captured.err
        assert "cockpit_error_should_reach_console_and_file" in log_path.read_text(
            encoding="utf-8",
        )

    def test_cockpit_via_env_var_matches_explicit_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    ):
        monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
        monkeypatch.setenv(
            "JARVIS_AEGIS_DAEMON_LOG_PATH", str(tmp_path / "daemon.log"),
        )
        logging.getLogger().handlers = []
        daemon_mod._configure_daemon_logging()
        root = logging.getLogger()
        handlers = _console_handlers(root)
        assert handlers[0].level == logging.ERROR

    def test_cockpit_file_handler_install_failure_fails_soft(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    ):
        """A bad log path (parent dir unwritable / not creatable) must
        never block daemon boot -- console-only ERROR+ is the fallback."""
        # Point at a path whose parent is a FILE (not a dir) -- mkdir +
        # FileHandler open both fail deterministically without touching
        # OS permission bits (portable across CI sandboxes).
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("blocker")
        bad_log_path = blocker / "daemon.log"
        monkeypatch.setenv("JARVIS_AEGIS_DAEMON_LOG_PATH", str(bad_log_path))
        logging.getLogger().handlers = []

        daemon_mod._configure_daemon_logging(PresentationMode.COCKPIT)  # must not raise

        root = logging.getLogger()
        assert len(_console_handlers(root)) == 1
        assert _console_handlers(root)[0].level == logging.ERROR
        assert not _file_handlers(root)
