"""The secret gate sees what a text scanner on the receiving end sees.

GitGuardian opened an incident on a PEM-shaped fixture in
``tests/governance/test_egress_redactor.py`` once it reached ``main``. Three
gaps let it through, each pinned here:

  1. the AST scanner only judged ASSIGNED values, so a shape in a parametrize
     list or a positional argument was invisible to it;
  2. it ran only in CI -- after the push, when the bytes were already public;
  3. nothing scanned per COMMIT, and a remote scanner reads every commit.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import fake_credentials as fake

_REPO = Path(__file__).resolve().parents[2]
_SCANNER = _REPO / ".github" / "scripts" / "scan_secrets.py"
_HOOK = _REPO / "scripts" / "hooks" / "pre-push"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


scan_secrets = _load("scan_secrets", _SCANNER)


def _kinds(source: str) -> list:
    return [f.kind for f in scan_secrets.scan_source(source, "t.py")]


# ---------------------------------------------------------------------------
# 1. Shapes in every non-docstring literal
# ---------------------------------------------------------------------------


def test_a_shape_in_a_parametrize_list_is_flagged():
    src = f'@pytest.mark.parametrize("s", ["{fake.AWS_ACCESS_KEY}"])\ndef t(s): ...\n'
    assert _kinds(src) == ["AWS Access Key"]


def test_a_shape_in_a_positional_argument_is_flagged():
    assert _kinds(f'scrub("{fake.GOOGLE_API_KEY}")\n') == ["Google API Key"]


def test_a_pem_marker_in_a_multiline_literal_is_flagged():
    body = fake.PEM_PRIVATE_KEY.replace("\n", "\\n")
    assert _kinds(f'check(\n    "{body}"\n)\n') == ["PEM Private Key"]


def test_a_key_in_a_docstring_is_no_longer_exempt():
    """Docstrings are scanned as prose now (tests/security/test_prose_secret_scan.py)."""
    src = f'def f():\n    """Detects {fake.AWS_ACCESS_KEY} shapes."""\n'
    assert _kinds(src) == ["AWS Access Key"]


def test_the_pragma_covers_every_line_of_a_multiline_literal():
    src = (
        "check(\n"
        f'    "{fake.SLACK_TOKEN}"\n'
        "    \"tail\"  # pragma: allowlist secret\n"
        ")\n"
    )
    assert _kinds(src) == []


def test_an_assigned_value_is_reported_once_not_twice():
    assert _kinds(f'KEY = "{fake.AWS_ACCESS_KEY}"\n') == ["AWS Access Key"]


def test_fragment_assembly_leaves_nothing_on_disk_yet_keeps_the_shape():
    """The whole point of fake_credentials: every value is a real shape at
    runtime, and the file that builds them contains none."""
    # The six whose formats the CI scanner itself defines; the GitHub and
    # Anthropic fakes follow the redactor's looser shapes and are not.
    for value in (
        fake.AWS_ACCESS_KEY, fake.SLACK_TOKEN, fake.OPENAI_KEY,
        fake.GOOGLE_API_KEY, fake.JWT, fake.PEM_PRIVATE_KEY,
    ):
        assert scan_secrets.matched_shape(value), value[:12]
    source = (_REPO / "tests" / "support" / "fake_credentials.py").read_text()
    assert scan_secrets.scan_source(source, "fake_credentials.py") == []


def test_the_repository_scans_clean():
    """What CI runs. It was red on main before this change."""
    assert [f.to_dict() for f in scan_secrets.scan_tree(_REPO)] == []


# ---------------------------------------------------------------------------
# 2/3. Per-commit scanning, and the pre-push gate
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        check=True, env=env,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (r / ".github" / "scripts").mkdir(parents=True)
    shutil.copy(_SCANNER, r / ".github" / "scripts" / "scan_secrets.py")
    (r / "clean.py").write_text("X = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _commit(repo: Path, message: str, **files: str) -> str:
    for name, content in files.items():
        path = repo / name
        if content is None:
            path.unlink()
        else:
            path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def test_a_secret_deleted_by_a_later_commit_is_still_caught(repo):
    base = _git(repo, "rev-parse", "HEAD")
    leaked = _commit(repo, "leak", **{"cfg.py": f'KEY = "{fake.AWS_ACCESS_KEY}"\n'})
    _commit(repo, "remove", **{"cfg.py": None})
    findings = scan_secrets.scan_revisions(repo, [f"{base}..HEAD"])
    assert [f.path for f in findings] == [f"cfg.py@{leaked[:10]}"]
    # The tip alone is clean -- which is exactly why a tip scan is not enough.
    assert scan_secrets.scan_tree(repo) == []


def test_a_renamed_file_is_scanned_under_its_new_path(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "clean.py").write_text(f'X = "{fake.AWS_ACCESS_KEY}"\n')
    _git(repo, "mv", "clean.py", "moved.py")
    _git(repo, "commit", "-qam", "rename+leak")
    findings = scan_secrets.scan_revisions(repo, [f"{base}..HEAD"])
    assert [f.path.split("@")[0] for f in findings] == ["moved.py"]


def test_a_secret_introduced_by_a_merge_resolution_is_caught(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "side")
    _commit(repo, "side", **{"a.py": "A = 1\n"})
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main", **{"b.py": "B = 1\n"})
    _git(repo, "merge", "-q", "--no-commit", "side")
    (repo / "c.py").write_text(f'C = "{fake.SLACK_TOKEN}"\n')
    _git(repo, "add", "c.py")
    _git(repo, "commit", "-qm", "evil merge")
    findings = scan_secrets.scan_revisions(repo, [f"{base}..HEAD"])
    assert [f.path.split("@")[0] for f in findings] == ["c.py"]


def _push_hook(repo: Path, url: str, stdin: str, hooks_dir: Path = None):
    env = dict(os.environ)
    if hooks_dir is not None:
        _git(repo, "config", "core.hooksPath", str(hooks_dir))
    return subprocess.run(
        [sys.executable, str(_HOOK), "origin", url], input=stdin.encode(),
        cwd=repo, capture_output=True, env=env, check=False,
    )


def _update_line(repo: Path, remote_sha: str = "0" * 40) -> str:
    head = _git(repo, "rev-parse", "HEAD")
    return f"refs/heads/main {head} refs/heads/main {remote_sha}\n"


def test_the_hook_refuses_a_push_that_carries_a_credential(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "leak", **{"cfg.py": f'KEY = "{fake.OPENAI_KEY}"\n'})
    r = _push_hook(repo, "https://example.invalid/x.git", _update_line(repo, base))
    assert r.returncode == 1
    assert b"OpenAI Key" in r.stderr and b"cfg.py" in r.stderr


def test_the_hook_passes_a_clean_push(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "ok", **{"ok.py": "Y = 2\n"})
    r = _push_hook(repo, "git@github.com:o/r.git", _update_line(repo, base))
    assert r.returncode == 0, r.stderr


def test_a_new_branch_is_scanned_back_to_what_the_remote_has(repo):
    _commit(repo, "leak", **{"cfg.py": f'KEY = "{fake.OPENAI_KEY}"\n'})
    # No remote-tracking refs at all: every local commit is unpublished.
    r = _push_hook(repo, "https://example.invalid/x.git", _update_line(repo))
    assert r.returncode == 1


@pytest.mark.parametrize("url", [
    "/mnt/c/Users/x/jarvis", "../mirror", "file:///srv/mirror.git", "C:/mirror",
])
def test_a_push_to_a_local_path_is_not_an_exposure(repo, url):
    _commit(repo, "leak", **{"cfg.py": f'KEY = "{fake.OPENAI_KEY}"\n'})
    assert _push_hook(repo, url, _update_line(repo)).returncode == 0


def test_a_branch_deletion_scans_nothing(repo):
    line = f"(delete) {'0' * 40} refs/heads/gone {_git(repo, 'rev-parse', 'HEAD')}\n"
    assert _push_hook(repo, "https://example.invalid/x.git", line).returncode == 0


def test_the_hook_chains_to_a_preserved_local_hook(repo, tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    local = hooks / "pre-push.local"
    local.write_text("#!/bin/sh\ncat > /dev/null\nexit 7\n")
    local.chmod(0o755)
    base = _git(repo, "rev-parse", "HEAD")
    r = _push_hook(repo, "https://example.invalid/x.git", _update_line(repo, base), hooks)
    assert r.returncode == 7, "a preserved guard must still run -- and decide"


def test_the_hook_fails_closed_when_the_scanner_is_broken(repo):
    (repo / ".github" / "scripts" / "scan_secrets.py").write_text("raise RuntimeError('x')\n")
    r = _push_hook(repo, "https://example.invalid/x.git", _update_line(repo))
    assert r.returncode == 1 and b"fail closed" in r.stderr


# ---------------------------------------------------------------------------
# The installer chains instead of overwriting
# ---------------------------------------------------------------------------


def test_install_preserves_a_foreign_pre_push_and_remove_restores_it(tmp_path):
    installer = _load("install_hooks", _REPO / "scripts" / "install_hooks.py")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    guard = "#!/bin/sh\n# main-branch protection\nexit 0\n"
    (hooks / "pre-push").write_text(guard)

    ok, _ = installer.install_hook(hooks, _HOOK.parent, "pre-push")
    assert ok
    assert (hooks / "pre-push.local").read_text() == guard
    assert "JARVIS" in (hooks / "pre-push").read_text()

    ok, _ = installer.remove_hook(hooks, "pre-push")
    assert ok and (hooks / "pre-push").read_text() == guard
    assert not (hooks / "pre-push.local").exists()


def test_reinstalling_does_not_chain_the_gate_to_itself(tmp_path):
    installer = _load("install_hooks", _REPO / "scripts" / "install_hooks.py")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    installer.install_hook(hooks, _HOOK.parent, "pre-push")
    installer.install_hook(hooks, _HOOK.parent, "pre-push")
    assert not (hooks / "pre-push.local").exists()


def test_an_unknown_hook_name_is_refused_not_ignored():
    installer = _load("install_hooks", _REPO / "scripts" / "install_hooks.py")
    with pytest.raises(SystemExit):
        installer._selected(["pre-psuh"])


def test_an_old_checkout_falls_back_to_the_pushed_commits_scanner(repo):
    """Pushing from a branch whose working-tree scanner predates per-commit
    scanning must neither refuse every push nor go blind."""
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "leak", **{"cfg.py": f'KEY = "{fake.OPENAI_KEY}"\n'})
    (repo / ".github" / "scripts" / "scan_secrets.py").write_text(
        "ALLOWLIST_PRAGMA = 'x'\n",   # an older scanner: no scan_revisions
    )
    r = _push_hook(repo, "https://example.invalid/x.git", _update_line(repo, base))
    assert r.returncode == 1 and b"OpenAI Key" in r.stderr


def test_no_capable_scanner_anywhere_is_reported_not_refused(tmp_path):
    r = tmp_path / "bare"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (r / "a.py").write_text("A = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "no scanner in this repository")
    out = _push_hook(r, "https://example.invalid/x.git", _update_line(r))
    assert out.returncode == 0 and b"not applicable" in out.stderr
