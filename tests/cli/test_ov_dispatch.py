"""Tests for the ov dispatcher (backend/core/ouroboros/cli/ov.py).

TDD: written before the implementation. Pins the DRY-delegation contract from
spec 2026-07-06 §4: ov subcommands translate to legacy argv (no flag
re-parsing), status/attach/help are handled without booting the organism.
"""
from __future__ import annotations

from backend.core.ouroboros.cli import ov
from backend.core.ouroboros.cli.ov import resolve


class TestResolveSubcommands:
    def test_no_args_is_cockpit_with_empty_argv(self) -> None:
        inv = resolve([])
        assert inv.action == "cockpit"
        assert inv.delegate_argv == []

    def test_bare_flags_without_verb_are_cockpit_passthrough(self) -> None:
        inv = resolve(["--cost-cap", "1.0", "-v"])
        assert inv.action == "cockpit"
        assert inv.delegate_argv == ["--cost-cap", "1.0", "-v"]

    def test_explicit_cockpit_verb_strips_the_verb(self) -> None:
        inv = resolve(["cockpit", "-v"])
        assert inv.action == "cockpit"
        assert inv.delegate_argv == ["-v"]

    def test_run_maps_to_headless(self) -> None:
        inv = resolve(["run"])
        assert inv.action == "headless"
        assert inv.delegate_argv == ["--headless"]

    def test_run_forwards_flags_after_headless(self) -> None:
        inv = resolve(["run", "--cost-cap", "2.0", "-v"])
        assert inv.action == "headless"
        assert inv.delegate_argv == ["--headless", "--cost-cap", "2.0", "-v"]

    def test_daemon_maps_to_headless(self) -> None:
        inv = resolve(["daemon"])
        assert inv.action == "headless"
        assert inv.delegate_argv == ["--headless"]

    def test_status_action(self) -> None:
        assert resolve(["status"]).action == "status"

    def test_attach_is_stub_with_message(self) -> None:
        inv = resolve(["attach"])
        assert inv.action == "attach"
        assert inv.message  # non-empty "coming soon" notice

    def test_help_variants(self) -> None:
        for a in (["help"], ["--help"], ["-h"]):
            assert resolve(a).action == "help"


class TestStatusDigest:
    def test_uses_provider_line_when_present(self) -> None:
        line = ov.status_digest(provider=lambda: "apply=NOTIFY/2 verify=3/3")
        assert "verify=3/3" in line

    def test_no_prior_session_when_provider_returns_none(self) -> None:
        assert "no prior session" in ov.status_digest(provider=lambda: None).lower()

    def test_never_raises_on_broken_provider(self) -> None:
        def boom() -> str:
            raise RuntimeError("nope")

        # Must degrade to a string, not propagate.
        assert isinstance(ov.status_digest(provider=boom), str)


class TestMainWithoutBoot:
    """main() paths that must NOT boot the organism."""

    def test_help_returns_zero(self, capsys) -> None:
        assert ov.main(["help"]) == 0
        assert "ov" in capsys.readouterr().out.lower()

    def test_attach_returns_zero_and_explains(self, capsys) -> None:
        assert ov.main(["attach"]) == 0
        assert "attach" in capsys.readouterr().out.lower()

    def test_status_returns_zero(self, capsys) -> None:
        assert ov.main(["status"]) == 0
        capsys.readouterr()  # drain
