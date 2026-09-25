#!/usr/bin/env python3
"""
JARVIS Pre-Push Secret Gate
===========================

Scans every commit about to leave this machine for credential-shaped values,
with the same AST + shape scanner CI runs (``.github/scripts/scan_secrets.py``)
-- but BEFORE the push, which is the only point where it can still help.

Why it exists: that scanner ran only in CI, after the push. A PEM-shaped test
fixture reached ``main`` and GitGuardian opened an incident on it; by the time
CI could have said anything, the bytes were already public. A text scanner on
the receiving end reads every pushed COMMIT, so this scans each commit's file
versions, not just the tip.

Scope: only pushes to a NETWORK remote are an exposure. The post-commit mirror
into the local Windows clone is a filesystem push and is not scanned -- doing
so would put a scan on every commit for no protection.

Chaining: an existing, non-JARVIS ``pre-push`` hook is preserved by the
installer as ``pre-push.local`` and run after this gate passes, with the same
arguments and stdin -- so a local main-branch guard is never silently dropped.

Fails CLOSED: if the scanner itself cannot run, the push is refused. Bypass a
reviewed false positive with ``# pragma: allowlist secret`` on the line, or the
whole gate with ``git push --no-verify``.

Runs under any Python 3.8+ on any host: git invokes the ``pre-push`` shell
wrapper, which resolves a working interpreter (native, uv-managed, or the WSL
bridge on Windows) and runs this file with the hook's arguments and stdin.

Install:  python3 scripts/install_hooks.py install pre-push
"""
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

_ZERO = re.compile(r"^0+$")
#: user@host:path -- git's scp-like syntax for an SSH remote.
_SCP_LIKE = re.compile(r"^[^/\s:@]+@[^/\s:]+:")


def _git(*args: str) -> str:
    # UTF-8, not the locale: on Windows the locale codec (cp1252) cannot
    # decode a non-ASCII path, and a decode error here would fail the push
    # closed for no reason.
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace",
    ).stdout.strip()


def _is_network_remote(url: str) -> bool:
    """True unless *url* is a path on this machine."""
    if "://" in url:
        return not url.startswith("file://")
    if _SCP_LIKE.match(url):
        return True
    return False  # an absolute/relative path, or a Windows drive path


def _revs_for(local_sha: str, remote_sha: str, remote: str) -> list:
    """rev-list arguments selecting exactly the commits this ref update sends."""
    if not _ZERO.match(remote_sha) and _git("cat-file", "-t", remote_sha) == "commit":
        return [local_sha, "^" + remote_sha]
    # A new branch, or a remote tip we have not fetched: everything not
    # already on the remote. A push to a bare URL has no remote-tracking refs
    # of its own, so fall back to "not on ANY remote" rather than scanning the
    # branch's whole history.
    named = remote in _git("remote").split()
    return [local_sha, "--not", f"--remotes={remote}" if named else "--remotes"]


_SCANNER_PATH = ".github/scripts/scan_secrets.py"


def _module_from_source(source: str, origin: str):
    module = types.ModuleType("scan_secrets")
    module.__file__ = origin
    sys.modules["scan_secrets"] = module
    exec(compile(source, origin, "exec"), module.__dict__)  # noqa: S102
    return module


#: The scanner snapshot the installer places beside this file.
_SNAPSHOT = Path(__file__).resolve().parent / "scan_secrets.py"


def _load_scanners(root: Path, pushed: list) -> list:
    """Every scanner this gate TRUSTS and that can judge a push, as
    ``(origin, module)``. Empty only when no version anywhere is capable.

    Trusted, in order: the snapshot installed beside this gate, and the
    checkout's own copy. The gate runs ALL of them and reports the union, so a
    scanner weakened in the working tree cannot hide what the snapshot sees,
    and a checkout on an old branch (whose copy predates per-commit scanning)
    cannot blind the gate. Measured live: with only the checkout and the
    pushed commits to draw on, re-pushing the original leak from the Windows
    clone's old branch passed as "not applicable".

    The copy INSIDE the pushed commits is the last resort, used only when no
    trusted scanner is capable: those are the very commits under inspection.

    A scanner that fails to LOAD raises -- a broken gate; the caller fails
    closed.
    """
    trusted = []
    if _SNAPSHOT.is_file():
        trusted.append((_SNAPSHOT.read_text(encoding="utf-8"), f"installed {_SNAPSHOT.name}"))
    path = root / _SCANNER_PATH
    if path.is_file():
        trusted.append((path.read_text(encoding="utf-8"), str(path)))
    scanners = []
    for source, origin in trusted:
        module = _module_from_source(source, origin)
        if hasattr(module, "scan_revisions"):
            scanners.append((origin, module))
    if scanners:
        return scanners
    for sha in pushed:
        # Bytes, decoded as UTF-8: the Windows locale codec (cp1252) cannot
        # decode this file, and text=True would hand compile() None.
        blob = subprocess.run(
            ["git", "show", f"{sha}:{_SCANNER_PATH}"],
            capture_output=True, check=False,
        )
        if blob.returncode == 0:
            module = _module_from_source(
                blob.stdout.decode("utf-8"), f"{sha[:10]}:{_SCANNER_PATH}",
            )
            if hasattr(module, "scan_revisions"):
                return [(module.__file__, module)]
    return []


def _chain(argv: list, stdin: bytes) -> int:
    local = Path(_git("rev-parse", "--git-path", "hooks")) / "pre-push.local"
    if not local.is_file():
        return 0
    if os.name == "nt":
        # Windows cannot exec a hook script directly; git itself runs hooks
        # through its bundled sh, which honours the shebang. Do the same.
        sh = shutil.which("sh")
        if sh is None:
            sys.stderr.write(
                f"[pre-push] cannot run chained {local.name}: no sh on PATH "
                "-- push refused (fail closed)\n",
            )
            return 1
        return subprocess.run([sh, str(local), *argv], input=stdin, check=False).returncode
    if os.access(local, os.X_OK):
        return subprocess.run([str(local), *argv], input=stdin, check=False).returncode
    return 0


def main() -> int:
    # A console that cannot encode a character must degrade the message, not
    # crash the gate (which would refuse the push for a cosmetic reason).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = sys.argv[1:]
    remote = argv[0] if argv else ""
    url = argv[1] if len(argv) > 1 else remote
    stdin = sys.stdin.buffer.read()

    if _is_network_remote(url):
        root = Path(_git("rev-parse", "--show-toplevel") or ".")
        updates = [
            parts for parts in (
                line.split() for line in stdin.decode("utf-8", "replace").splitlines()
            )
            # malformed lines and branch deletions carry nothing to scan
            if len(parts) == 4 and not _ZERO.match(parts[1])
        ]
        try:
            scanners = _load_scanners(root, [parts[1] for parts in updates])
            if not scanners:
                sys.stderr.write(
                    "[pre-push] no per-commit secret scanner installed, in this "
                    "checkout, or in the pushed commits -- secret gate not "
                    "applicable (reinstall: scripts/install_hooks.py install pre-push)\n",
                )
            else:
                seen, findings = set(), []
                for parts in updates:
                    revs = _revs_for(parts[1], parts[3], remote)
                    for _origin, scanner in scanners:
                        for f in scanner.scan_revisions(root, revs):
                            key = (f.path, f.line, f.kind)
                            if key not in seen:
                                seen.add(key)
                                findings.append(f)
                if findings:
                    sys.stderr.write(
                        f"\n\033[1;31m[pre-push] {len(findings)} credential-shaped "
                        f"value(s) in the commits being pushed to {remote}:\033[0m\n",
                    )
                    for f in findings:
                        sys.stderr.write(f"   {f.kind} - {f.path}:{f.line} ({f.name})\n")
                    sys.stderr.write(
                        "\n   A test fixture must never contain a complete credential:\n"
                        "   build it from fragments (tests/support/fake_credentials.py).\n"
                        "   A value already pushed stays in history -- rewrite the\n"
                        "   unpushed commits, not just the tip.\n"
                        f"   Reviewed false positive: '# {scanners[0][1].ALLOWLIST_PRAGMA}'.\n\n",
                    )
                    return 1
        except Exception as exc:  # noqa: BLE001 -- fail closed
            sys.stderr.write(
                f"\033[1;31m[pre-push] secret gate failed to run: "
                f"{type(exc).__name__}: {exc} -- push refused (fail closed)\033[0m\n",
            )
            return 1
    return _chain(argv, stdin)


if __name__ == "__main__":
    sys.exit(main())
