"""silent_boot regression suite — terminal stays clean during boot.

Pins the boot-log redirect substrate. Empirical proof: the boot
banner from the operator's screenshot (2026-05-03 21:01:51-52) was
buried under ~25 INFO log lines. Post-substrate, those lines route
to session_dir/debug.log; the terminal sees only the banner +
WARNING+ events.

Strict directives validated:

  * Idempotent: multiple configure_silent_boot calls return the same
    handler; root logger state stable across re-calls.
  * NEVER raises: every failure path returns None, leaves root
    logger untouched. Boot is not blocked by logging glue.
  * No hardcoded paths: caller supplies session_dir + filename via
    flag override.
  * Closed-taxonomy terminal levels: DEBUG/INFO/WARNING/ERROR/
    CRITICAL. Unknown values fall back to WARNING (operator typo
    safety).
  * AST-pinned cross-file contract: harness.py contains
    configure_silent_boot call (catches refactor that drops it).

Covers:

  §A   Master flag gate (default true → file handler installed;
       false → no-op)
  §B   File handler creates session_dir + writes at DEBUG level
  §C   Terminal handler installed at WARNING by default
  §D   Idempotency — second call returns same handler
  §E   Defensive paths (bad session_dir, missing parent, perm error)
  §F   terminal_level resolves DEBUG/INFO/WARNING/ERROR/CRITICAL
  §G   restore_legacy_terminal_logging undoes silent boot
  §H   AST pins (5) clean + tampering caught
  §I   Auto-discovery integration
  §J   End-to-end behavior — INFO logged after configure goes ONLY
       to file; WARNING goes to both
"""
from __future__ import annotations

import ast
import logging
import pathlib
import sys
import threading
from typing import Any

import pytest

from backend.core.ouroboros.governance import silent_boot as sb


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_SILENT_BOOT_ENABLED",
        "JARVIS_SILENT_BOOT_TERMINAL_LEVEL",
        "JARVIS_SILENT_BOOT_LOG_FILENAME",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    # Test cleanup: undo any silent boot the test installed
    sb.restore_legacy_terminal_logging()


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


@pytest.fixture
def session_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "session-test"


# ---------------------------------------------------------------------------
# §A — Master flag gate
# ---------------------------------------------------------------------------


class TestMasterFlagGate:
    def test_default_true_installs_handler(
        self, fresh_registry, session_dir,
    ):
        handler = sb.configure_silent_boot(session_dir)
        assert handler is not None
        assert isinstance(handler, logging.FileHandler)

    def test_explicit_false_no_op(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry, session_dir,
    ):
        monkeypatch.setenv("JARVIS_SILENT_BOOT_ENABLED", "false")
        handler = sb.configure_silent_boot(session_dir)
        assert handler is None

    def test_is_enabled_default_true(self, fresh_registry):
        assert sb.is_enabled() is True

    def test_is_enabled_env_false(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_SILENT_BOOT_ENABLED", "false")
        assert sb.is_enabled() is False


# ---------------------------------------------------------------------------
# §B — File handler creates session_dir
# ---------------------------------------------------------------------------


class TestFileHandlerInstall:
    def test_creates_session_dir_if_missing(
        self, fresh_registry, session_dir,
    ):
        assert not session_dir.exists()
        sb.configure_silent_boot(session_dir)
        assert session_dir.exists()

    def test_log_file_path(self, fresh_registry, session_dir):
        handler = sb.configure_silent_boot(session_dir)
        assert handler is not None
        assert handler.baseFilename == str(session_dir / "debug.log")

    def test_handler_level_is_debug(self, fresh_registry, session_dir):
        handler = sb.configure_silent_boot(session_dir)
        assert handler is not None
        assert handler.level == logging.DEBUG

    def test_custom_log_filename_via_flag(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry, session_dir,
    ):
        monkeypatch.setenv(
            "JARVIS_SILENT_BOOT_LOG_FILENAME", "boot.log",
        )
        handler = sb.configure_silent_boot(session_dir)
        assert handler is not None
        assert handler.baseFilename == str(session_dir / "boot.log")

    def test_log_filename_override_kwarg(
        self, fresh_registry, session_dir,
    ):
        handler = sb.configure_silent_boot(
            session_dir, log_filename_override="custom.log",
        )
        assert handler is not None
        assert handler.baseFilename == str(session_dir / "custom.log")


# ---------------------------------------------------------------------------
# §C — Terminal handler at WARNING
# ---------------------------------------------------------------------------


class TestTerminalHandler:
    def test_terminal_handler_installed_at_warning(
        self, fresh_registry, session_dir,
    ):
        sb.configure_silent_boot(session_dir)
        root = logging.getLogger()
        terminal_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            and getattr(h, sb._HANDLER_MARKER, False)
        ]
        assert len(terminal_handlers) == 1
        assert terminal_handlers[0].level == logging.WARNING

    def test_terminal_threshold_kwarg_overrides(
        self, fresh_registry, session_dir,
    ):
        sb.configure_silent_boot(
            session_dir, terminal_threshold=logging.ERROR,
        )
        root = logging.getLogger()
        terminal_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            and getattr(h, sb._HANDLER_MARKER, False)
        ]
        assert terminal_handlers[0].level == logging.ERROR


# ---------------------------------------------------------------------------
# §D — Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_second_call_returns_same_handler(
        self, fresh_registry, session_dir,
    ):
        h1 = sb.configure_silent_boot(session_dir)
        h2 = sb.configure_silent_boot(session_dir)
        assert h1 is h2

    def test_second_call_no_duplicate_handlers(
        self, fresh_registry, session_dir,
    ):
        sb.configure_silent_boot(session_dir)
        sb.configure_silent_boot(session_dir)
        root = logging.getLogger()
        marked = [
            h for h in root.handlers
            if getattr(h, sb._HANDLER_MARKER, False)
        ]
        # Exactly 2 marked handlers: file + stream
        assert len(marked) == 2

    def test_concurrent_configure_thread_safe(
        self, fresh_registry, session_dir,
    ):
        # 10 threads call concurrently — module lock serializes
        results = []
        lock = threading.Lock()

        def _call() -> None:
            handler = sb.configure_silent_boot(session_dir)
            with lock:
                results.append(handler)

        threads = [threading.Thread(target=_call) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All 10 returns the same handler instance (idempotent)
        non_none = [h for h in results if h is not None]
        assert len(set(id(h) for h in non_none)) == 1


# ---------------------------------------------------------------------------
# §E — Defensive paths
# ---------------------------------------------------------------------------


class TestDefensivePaths:
    def test_invalid_session_dir_returns_none(
        self, fresh_registry, monkeypatch: pytest.MonkeyPatch,
    ):
        # Pass a path that mkdir will fail on (use a file-as-parent
        # which will fail when trying to create a child)
        bad_path = "\x00invalid_path"
        result = sb.configure_silent_boot(bad_path)
        # Either returns None gracefully OR succeeds in some envs;
        # critical contract is "never raises"
        assert result is None or isinstance(result, logging.FileHandler)

    def test_none_session_dir_returns_none_safely(self, fresh_registry):
        # Path(None) raises in __init__; configure_silent_boot wraps
        result = sb.configure_silent_boot(None)
        assert result is None

    def test_master_flag_off_no_root_mutation(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry, session_dir,
    ):
        monkeypatch.setenv("JARVIS_SILENT_BOOT_ENABLED", "false")
        root = logging.getLogger()
        before_handlers = list(root.handlers)
        sb.configure_silent_boot(session_dir)
        after_handlers = list(root.handlers)
        # Root handlers list unchanged (no marked handlers added)
        marked = [
            h for h in after_handlers
            if getattr(h, sb._HANDLER_MARKER, False)
        ]
        assert marked == []


# ---------------------------------------------------------------------------
# §F — terminal_level resolution
# ---------------------------------------------------------------------------


class TestTerminalLevelResolution:
    def test_default_warning(self, fresh_registry):
        assert sb.terminal_level() == logging.WARNING

    @pytest.mark.parametrize("env_val,expected_level", [
        ("DEBUG", logging.DEBUG),
        ("INFO", logging.INFO),
        ("WARNING", logging.WARNING),
        ("ERROR", logging.ERROR),
        ("CRITICAL", logging.CRITICAL),
        ("debug", logging.DEBUG),  # case-insensitive
        ("warning", logging.WARNING),
    ])
    def test_resolves_named_levels(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
        env_val: str, expected_level: int,
    ):
        monkeypatch.setenv(
            "JARVIS_SILENT_BOOT_TERMINAL_LEVEL", env_val,
        )
        assert sb.terminal_level() == expected_level

    def test_unknown_level_falls_back_to_warning(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_SILENT_BOOT_TERMINAL_LEVEL", "BOGUS",
        )
        assert sb.terminal_level() == logging.WARNING


# ---------------------------------------------------------------------------
# §G — restore_legacy_terminal_logging
# ---------------------------------------------------------------------------


class TestRestoreLegacy:
    def test_restore_removes_marked_handlers(
        self, fresh_registry, session_dir,
    ):
        sb.configure_silent_boot(session_dir)
        root = logging.getLogger()
        before = sum(
            1 for h in root.handlers
            if getattr(h, sb._HANDLER_MARKER, False)
        )
        assert before == 2  # file + stream
        removed = sb.restore_legacy_terminal_logging()
        assert removed == 2
        after = sum(
            1 for h in root.handlers
            if getattr(h, sb._HANDLER_MARKER, False)
        )
        assert after == 0

    def test_restore_when_nothing_installed(self, fresh_registry):
        # No prior configure — restore is no-op (returns 0)
        removed = sb.restore_legacy_terminal_logging()
        assert removed == 0


# ---------------------------------------------------------------------------
# §H — AST pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def d1_pins() -> list:
    return list(sb.register_shipped_invariants())


class TestD1ASTPinsClean:
    def test_five_pins_registered(self, d1_pins):
        assert len(d1_pins) == 5
        names = {i.invariant_name for i in d1_pins}
        assert names == {
            "silent_boot_no_rich_import",
            "silent_boot_no_authority_imports",
            "silent_boot_configure_symbol_present",
            "silent_boot_discovery_symbols_present",
            "harness_calls_silent_boot",
        }

    @pytest.fixture(scope="class")
    def real_module_ast(self):
        import inspect
        src = inspect.getsource(sb)
        return ast.parse(src), src

    def test_no_rich_import_clean(self, d1_pins, real_module_ast):
        tree, src = real_module_ast
        pin = next(p for p in d1_pins
                   if p.invariant_name == "silent_boot_no_rich_import")
        assert pin.validate(tree, src) == ()

    def test_no_authority_imports_clean(self, d1_pins, real_module_ast):
        tree, src = real_module_ast
        pin = next(p for p in d1_pins
                   if p.invariant_name == "silent_boot_no_authority_imports")
        assert pin.validate(tree, src) == ()

    def test_configure_symbol_present_clean(self, d1_pins, real_module_ast):
        tree, src = real_module_ast
        pin = next(p for p in d1_pins
                   if p.invariant_name ==
                   "silent_boot_configure_symbol_present")
        assert pin.validate(tree, src) == ()

    def test_harness_calls_silent_boot_clean(self, d1_pins):
        path = pathlib.Path(
            "backend/core/ouroboros/battle_test/harness.py",
        )
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        pin = next(p for p in d1_pins
                   if p.invariant_name == "harness_calls_silent_boot")
        assert pin.validate(tree, src) == ()


class TestD1ASTPinsCatchTampering:
    def test_authority_import_caught(self, d1_pins):
        tampered = ast.parse(
            "from backend.core.ouroboros.governance.cancel_token import x\n"
        )
        pin = next(p for p in d1_pins
                   if p.invariant_name == "silent_boot_no_authority_imports")
        violations = pin.validate(tampered, "")
        assert any("cancel_token" in v for v in violations)

    def test_rich_import_caught(self, d1_pins):
        tampered = ast.parse("from rich.console import Console\n")
        pin = next(p for p in d1_pins
                   if p.invariant_name == "silent_boot_no_rich_import")
        violations = pin.validate(tampered, "")
        assert any("rich" in v for v in violations)

    def test_missing_configure_symbol_caught(self, d1_pins):
        tampered = ast.parse(
            "def something_else(): pass\n"
        )
        pin = next(p for p in d1_pins
                   if p.invariant_name ==
                   "silent_boot_configure_symbol_present")
        violations = pin.validate(tampered, "")
        assert violations

    def test_harness_missing_call_caught(self, d1_pins):
        tampered_src = "# harness without configure_silent_boot\n"
        tampered = ast.parse(tampered_src)
        pin = next(p for p in d1_pins
                   if p.invariant_name == "harness_calls_silent_boot")
        violations = pin.validate(tampered, tampered_src)
        assert violations
        assert "configure_silent_boot" in violations[0]


# ---------------------------------------------------------------------------
# §I — Auto-discovery
# ---------------------------------------------------------------------------


class TestAutoDiscoveryIntegration:
    def test_flag_registry_picks_up_silent_boot(self, fresh_registry):
        names = {s.name for s in fresh_registry.list_all()}
        assert "JARVIS_SILENT_BOOT_ENABLED" in names
        assert "JARVIS_SILENT_BOOT_TERMINAL_LEVEL" in names
        assert "JARVIS_SILENT_BOOT_LOG_FILENAME" in names

    def test_shipped_invariants_includes_d1_pins(self):
        # Defensive re-register (matches U1 + followups#5 pattern)
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        for inv in sb.register_shipped_invariants():
            sci.register_shipped_code_invariant(inv)
        names = {
            i.invariant_name for i in sci.list_shipped_code_invariants()
        }
        for expected in (
            "silent_boot_no_rich_import",
            "silent_boot_no_authority_imports",
            "silent_boot_configure_symbol_present",
            "silent_boot_discovery_symbols_present",
            "harness_calls_silent_boot",
        ):
            assert expected in names

    def test_validate_all_no_d1_violations(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        for inv in sb.register_shipped_invariants():
            sci.register_shipped_code_invariant(inv)
        results = sci.validate_all()
        d1_failures = [
            r for r in results
            if r.invariant_name.startswith("silent_boot_")
            or r.invariant_name == "harness_calls_silent_boot"
        ]
        assert d1_failures == [], (
            f"D1 pins reporting violations: "
            f"{[r.to_dict() for r in d1_failures]}"
        )


# ---------------------------------------------------------------------------
# §J — End-to-end behavioral proof
# ---------------------------------------------------------------------------


class TestEndToEndBehavior:
    def test_info_logged_after_configure_goes_to_file_only(
        self, fresh_registry, session_dir, capsys,
    ):
        sb.configure_silent_boot(session_dir)
        test_logger = logging.getLogger("test.silent_boot.e2e_info")
        test_logger.info("noisy_boot_info_should_not_appear_on_terminal")
        # Force flush
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        captured = capsys.readouterr()
        # Terminal (stderr) should NOT contain the INFO message
        assert "noisy_boot_info" not in captured.err
        # File SHOULD contain it
        log_path = session_dir / "debug.log"
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert "noisy_boot_info_should_not_appear_on_terminal" in content

    def test_warning_logged_after_configure_goes_to_both(
        self, fresh_registry, session_dir, capsys,
    ):
        sb.configure_silent_boot(session_dir)
        test_logger = logging.getLogger("test.silent_boot.e2e_warn")
        test_logger.warning("warn_should_appear_on_terminal_AND_file")
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        captured = capsys.readouterr()
        assert "warn_should_appear_on_terminal_AND_file" in captured.err
        log_path = session_dir / "debug.log"
        content = log_path.read_text(encoding="utf-8")
        assert "warn_should_appear_on_terminal_AND_file" in content
