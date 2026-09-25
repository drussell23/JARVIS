"""The pre-push launcher finds a Python that WORKS, on any host.

A ``#!/usr/bin/env python3`` hook fails with "not found" under Git for Windows
when ``python3`` is absent -- and this Windows host's ``python3`` and
``python`` are Microsoft Store aliases: present on PATH, failing only when run.
The launcher is sh (git runs every hook through one) and proves each
candidate by executing it.

Each test runs the REAL launcher against a stub gate that reports which
interpreter ran it and what arguments and stdin arrived, under a PATH built
for the case -- including a simulated Windows host whose only Python is the
WSL bridge.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_LAUNCHER = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "pre-push"

_STUB_GATE = '''\
import os, sys
data = sys.stdin.read()
print("RAN", os.path.realpath(sys.executable))
print("ARGS", sys.argv[1:])
print("STDIN", repr(data))
'''

#: Tools the launcher itself needs, independent of any Python.
_TOOLS = ("sh", "git", "uname", "dirname", "cat", "tr")


def _exe(path: Path, body: str) -> Path:
    # Replace, never write THROUGH: the fixture's tools are symlinks to the
    # real binaries, and writing to one would overwrite /usr/bin/<tool>.
    if path.is_symlink() or path.exists():
        path.unlink()
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture()
def hook(tmp_path: Path):
    """A hooks dir holding the real launcher and a stub gate, plus a bin dir
    containing only the tools the launcher needs and no Python at all."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy(_LAUNCHER, hooks / "pre-push")
    (hooks / "pre_push_gate.py").write_text(_STUB_GATE)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in _TOOLS:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    def run(*, env: dict = None, stdin: str = "refs/heads/x a refs/heads/x b\n"):
        full_env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
        full_env.update(env or {})
        return subprocess.run(
            ["sh", str(hooks / "pre-push"), "origin", "git@github.com:o/r.git"],
            input=stdin.encode(), cwd=repo, env=full_env,
            capture_output=True, check=False,
        )

    return type("Hook", (), {
        "dir": hooks, "bin": bin_dir, "repo": repo, "run": staticmethod(run),
    })


_REAL = os.path.realpath(sys.executable)


def test_a_store_alias_that_exists_but_fails_is_skipped(hook):
    _exe(hook.bin / "python3", "echo 'Python was not found' >&2; exit 49\n")
    (hook.bin / "python").symlink_to(sys.executable)
    r = hook.run()
    assert r.returncode == 0, r.stderr
    assert f"RAN {_REAL}".encode() in r.stdout


def test_arguments_and_stdin_reach_the_gate_intact(hook):
    (hook.bin / "python3").symlink_to(sys.executable)
    r = hook.run(stdin="refs/heads/a 1 refs/heads/a 2\nrefs/heads/b 3 refs/heads/b 4\n")
    assert b"ARGS ['origin', 'git@github.com:o/r.git']" in r.stdout
    assert b"refs/heads/b 3 refs/heads/b 4" in r.stdout, "a probe ate the ref list"


def test_the_operator_override_wins(hook, tmp_path):
    (hook.bin / "python3").symlink_to(sys.executable)
    chosen = tmp_path / "chosen-python"
    chosen.symlink_to(sys.executable)
    r = hook.run(env={"JARVIS_HOOK_PYTHON": str(chosen)})
    assert r.returncode == 0 and f"RAN {_REAL}".encode() in r.stdout


def test_a_broken_override_is_reported_and_resolution_continues(hook):
    (hook.bin / "python3").symlink_to(sys.executable)
    r = hook.run(env={"JARVIS_HOOK_PYTHON": "/nonexistent/python"})
    assert r.returncode == 0
    assert b"does not run Python" in r.stderr


def test_the_repository_config_override_is_honoured(hook, tmp_path):
    chosen = tmp_path / "cfg-python"
    chosen.symlink_to(sys.executable)
    subprocess.run(
        ["git", "-C", str(hook.repo), "config", "jarvis.hookPython", str(chosen)],
        check=True,
    )
    r = hook.run()
    assert r.returncode == 0 and f"RAN {_REAL}".encode() in r.stdout


def test_a_launcher_command_like_py_dash_3_is_word_split(hook):
    _exe(hook.bin / "py", f'[ "$1" = "-3" ] && shift; exec "{sys.executable}" "$@"\n')
    r = hook.run()
    assert r.returncode == 0 and f"RAN {_REAL}".encode() in r.stdout


def test_a_uv_managed_interpreter_is_found(hook):
    _exe(hook.bin / "uv", f'[ "$1 $2" = "python find" ] && echo "{sys.executable}"\n')
    r = hook.run()
    assert r.returncode == 0 and f"RAN {_REAL}".encode() in r.stdout


def test_no_interpreter_anywhere_refuses_the_push_with_instructions(hook):
    _exe(hook.bin / "python3", "exit 49\n")
    r = hook.run()
    assert r.returncode == 1
    assert b"no working Python" in r.stderr and b"jarvis.hookPython" in r.stderr


def test_a_missing_gate_refuses_the_push(hook):
    (hook.bin / "python3").symlink_to(sys.executable)
    (hook.dir / "pre_push_gate.py").unlink()
    r = hook.run()
    assert r.returncode == 1 and b"missing" in r.stderr


def test_on_windows_the_wsl_bridge_runs_the_gate(hook, tmp_path):
    """Simulated Windows host: uname says MINGW, every native Python is a
    Store alias, and wsl.exe is the only way in. The fake wsl.exe records
    what it was asked and serves `wslpath` and `python3` like the real one."""
    _exe(hook.bin / "uname", "echo MINGW64_NT-10.0-26200\n")
    _exe(hook.bin / "python3", "exit 49\n")
    log = tmp_path / "wsl.log"
    _exe(hook.bin / "wsl.exe", f'''
echo "argv: $* | conv=$MSYS_NO_PATHCONV/$MSYS2_ARG_CONV_EXCL" >> "{log}"
[ "$1" = "-e" ] && shift
case "$1" in
    # Like the real wsl.exe: every invocation forwards (and drains) stdin.
    wslpath) cat >/dev/null; echo "$3" ;;
    python3) shift; exec "{sys.executable}" "$@" ;;
esac
''')
    r = hook.run(stdin="refs/heads/a 1 refs/heads/a 2\n")
    assert r.returncode == 0, r.stderr
    assert b"ARGS ['origin', 'git@github.com:o/r.git']" in r.stdout
    assert b"refs/heads/a 1 refs/heads/a 2" in r.stdout, "a helper wsl.exe call ate the ref list"
    calls = log.read_text()
    assert "python3" in calls and "pre_push_gate.py" in calls
    assert "conv=1/*" in calls, "MSYS path conversion must be off for wsl.exe"


def test_a_named_wsl_distro_is_passed_through(hook, tmp_path):
    _exe(hook.bin / "uname", "echo MINGW64_NT\n")
    log = tmp_path / "wsl.log"
    _exe(hook.bin / "wsl.exe", f'''
echo "$*" >> "{log}"
[ "$1" = "-d" ] && shift 2
[ "$1" = "-e" ] && shift
case "$1" in
    wslpath) echo "$3" ;;
    python3) shift; exec "{sys.executable}" "$@" ;;
esac
''')
    r = hook.run(env={"JARVIS_HOOK_WSL_DISTRO": "Ubuntu"})
    assert r.returncode == 0, r.stderr
    assert all(line.startswith("-d Ubuntu") for line in log.read_text().splitlines())
