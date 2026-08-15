"""Regression spine for `ov link`.

Parsing is pure and tested without a filesystem; execution is tested against
real issued material. Every test names the operator mistake it prevents.
"""
from __future__ import annotations

import os
import stat

import pytest

from backend.core.ouroboros.cli import ov_link
from backend.core.ouroboros.cli.ov import resolve
from backend.core.ouroboros.governance import link_certs as lc

pytest.importorskip("cryptography")


class _Console:
    def __init__(self): self.lines = []
    def print(self, text, **kw): self.lines.append(str(text))
    @property
    def text(self): return "\n".join(self.lines)


# -- routing ---------------------------------------------------------------


def test_ov_routes_link_and_forwards_its_flags():
    inv = resolve(["link", "--connect", "engine.ts.net", "--port", "9000"])
    assert inv.action == "link"
    assert inv.delegate_argv == ["--connect", "engine.ts.net", "--port", "9000"]


def test_link_is_a_registered_verb_so_bare_ov_does_not_swallow_it():
    assert resolve(["link"]).action == "link"


# -- parsing ---------------------------------------------------------------


def test_an_unknown_flag_is_refused_with_a_suggestion():
    """Refuse, never silently ignore — `ov doctor`'s established posture."""
    args = ov_link.parse(["--serv"])
    assert args.error and "--serve" in args.error


def test_conflicting_modes_are_refused():
    args = ov_link.parse(["--serve", "--connect", "host"])
    assert args.error


def test_a_flag_missing_its_value_is_refused():
    assert ov_link.parse(["--port"]).error
    assert ov_link.parse(["--connect"]).error


def test_a_non_numeric_port_is_refused_at_parse_time():
    assert "integer" in ov_link.parse(["--port", "abc"]).error


def test_an_out_of_range_port_is_refused():
    assert ov_link.parse(["--port", "99999"]).error


def test_repeated_server_names_accumulate():
    args = ov_link.parse(["--issue-certs", "--server-name", "a",
                          "--server-name", "b"])
    assert args.server_names == ["a", "b"]


def test_parsing_reads_no_environment_and_touches_no_disk():
    """Pure, so routing is testable without a socket — ov.resolve's rule.

    Checked against the AST, not the text: the docstring legitimately
    contains the word "environment", and a substring match cannot tell an
    explanation from a use."""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(ov_link.parse)))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "environ" not in names and "getenv" not in names
    assert "open" not in names


# -- validation ------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "https://engine:9000", "engine:9000", "/etc/hosts", "a b", "",
])
def test_url_port_and_path_shapes_are_rejected_as_names(bad):
    """A wrong SAN fails at the handshake as a TRUST error, which reads as
    'certificates are broken' rather than 'you typed a URL'."""
    assert ov_link._looks_like_a_name(bad) is False


@pytest.mark.parametrize("good", ["engine.tailnet.ts.net", "100.64.1.2",
                                  "breden-pc", "::1", "fd7a:115c::1"])
def test_bare_hostnames_and_ip_literals_are_accepted(good):
    assert ov_link._looks_like_a_name(good) is True


def test_issue_without_a_server_name_exits_usage_not_a_traceback():
    console = _Console()
    assert ov_link.run_link(console, ["--issue-certs"]) == ov_link.EX_USAGE
    assert "server-name" in console.text


def test_connect_without_a_port_exits_usage():
    console = _Console()
    code = ov_link.run_link(console, ["--connect", "engine.ts.net"])
    assert code == ov_link.EX_USAGE


def test_no_mode_prints_help_and_exits_usage():
    console = _Console()
    assert ov_link.run_link(console, []) == ov_link.EX_USAGE
    assert "ov link" in console.text


def test_help_exits_zero():
    assert ov_link.run_link(_Console(), ["--help"]) == 0


# -- issuance --------------------------------------------------------------


def test_issue_writes_material_and_names_the_peer_bundle(tmp_path):
    console = _Console()
    code = ov_link.run_link(console, [
        "--issue-certs", "--dir", str(tmp_path / "m"),
        "--server-name", "engine.tailnet.ts.net", "--server-name", "100.64.1.2",
        "--client-name", "body-mac"])
    assert code == 0
    for name in lc.files_to_copy_to_peer():
        assert name in console.text
    assert lc.CA_KEY not in console.text, "the CA private key must not travel"


def test_reissue_without_force_exits_config_not_usage(tmp_path):
    """Distinct exit codes: a state-on-disk decision is not a typo."""
    args = ["--issue-certs", "--dir", str(tmp_path / "m"),
            "--server-name", "engine.ts.net"]
    assert ov_link.run_link(_Console(), args) == 0
    assert ov_link.run_link(_Console(), args) == ov_link.EX_CONFIG
    assert ov_link.run_link(_Console(), args + ["--force"]) == 0


def test_issued_keys_are_owner_only_through_the_cli(tmp_path):
    ov_link.run_link(_Console(), [
        "--issue-certs", "--dir", str(tmp_path / "m"),
        "--server-name", "engine.ts.net"])
    for name in (lc.CA_KEY, lc.SERVER_KEY, lc.CLIENT_KEY):
        mode = stat.S_IMODE(os.stat(tmp_path / "m" / name).st_mode)
        assert mode == 0o600


def test_concurrent_issuance_is_refused_rather_than_interleaved(tmp_path):
    """Two runs interleaving would produce a CA from one and leaves from the
    other — individually well-formed, collectively unverifiable."""
    d = tmp_path / "m"
    d.mkdir(parents=True)
    (d / ".issue.lock").write_text("99999")
    with pytest.raises(FileExistsError):
        lc.issue_link_material(directory=d, server_names=["engine.ts.net"])


def test_a_partial_write_never_becomes_a_readable_pem(tmp_path):
    """Published via durable_io.atomic_replace, so a reader building an SSL
    context sees the old file or the new one, never a half-written PEM."""
    import inspect
    assert "atomic_replace" in inspect.getsource(lc._publish)


# -- status ----------------------------------------------------------------


def test_status_reports_a_countdown_not_a_boolean(tmp_path):
    ov_link.run_link(_Console(), [
        "--issue-certs", "--dir", str(tmp_path / "m"),
        "--server-name", "engine.ts.net"])
    console = _Console()
    assert ov_link.run_link(console, ["--status", "--dir",
                                      str(tmp_path / "m")]) == 0
    assert "remaining" in console.text


def test_status_on_an_empty_directory_exits_config_with_a_remedy(tmp_path):
    console = _Console()
    code = ov_link.run_link(console, ["--status", "--dir", str(tmp_path / "e")])
    assert code == ov_link.EX_CONFIG
    assert "--issue-certs" in console.text


# -- serve/connect refuse rather than downgrade ----------------------------


def test_serve_without_material_exits_config_not_a_traceback(tmp_path,
                                                             monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_TLS_DIR", str(tmp_path / "absent"))
    console = _Console()
    code = ov_link.run_link(console, ["--serve", "--host", "127.0.0.1",
                                      "--port", "0"])
    assert code == ov_link.EX_CONFIG
    assert "mTLS" in console.text or "material" in console.text


def test_connect_with_a_url_shaped_host_exits_usage():
    console = _Console()
    code = ov_link.run_link(console, ["--connect", "https://engine:9000",
                                      "--port", "9000"])
    assert code == ov_link.EX_USAGE
