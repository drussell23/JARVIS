"""``ov link`` — issue the link's identity, serve it, or dial it.

Three verbs over the modules that already do the work. This file contains no
protocol logic, no certificate construction and no socket handling: it parses
arguments, validates them where a wrong value would produce an obscure
failure later, and reports what happened. Everything else is delegated.

WHY VALIDATION HAPPENS HERE
---------------------------
Two mistakes on this path fail far from their cause:

* **A wrong SAN.** A certificate issued for the wrong name fails at the
  handshake as a *trust* error, which reads as "certificates are broken"
  rather than "you typed the wrong hostname". So names are required and their
  shape is checked at the point of entry.
* **A missing peer.** ``--connect`` without a reachable host produces a
  connection error every backoff interval, forever, which looks like a
  network problem rather than a missing argument.

Both are refused with an exit code and a sentence, never a stack trace and
never a silent default. ``EX_USAGE`` (64) for an argument the operator can
fix; ``EX_CONFIG`` (78) for state on disk that needs a decision.

Follows ``ov doctor``'s established shape: a ``run_*`` entry point taking a
console and an argv slice, returning a process exit code, refusing unknown
flags rather than ignoring them.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.OvLink")

EX_USAGE = 64
EX_CONFIG = 78
EX_UNAVAILABLE = 69

LINK_HELP = """ov link -- the Body/Engine bridge

  ov link --issue-certs --server-name NAME [--server-name NAME ...]
                        [--client-name NAME] [--dir PATH] [--force]
      Issue this link's private CA and its two leaves. Run this ONCE, on the
      Engine. Every name the Body will dial the Engine by must be listed --
      its tailnet DNS name and its 100.x address are both normal.

  ov link --serve [--host ADDR] [--port N]
      Run the Engine side. Binds loopback unless --host says otherwise;
      point it at the tailnet address to accept the Body.

  ov link --connect HOST [--port N]
      Run the Body side. HOST must match a name in the Engine's certificate.

  ov link --status [--dir PATH]
      What material is installed, and how many days before it expires.

Flags:
  --dir PATH          certificate directory (default: JARVIS_LINK_TLS_DIR)
  --session ID        session identity to resume (default: JARVIS_LINK_SESSION)
  --node ID           this node's name (default: hostname)
  --force             re-issue over existing material (revokes the peer)
"""


def _hostname() -> str:
    try:
        import socket
        return socket.gethostname() or "node"
    except Exception:  # noqa: BLE001
        return "node"


def _looks_like_a_name(value: str) -> bool:
    """A hostname or IP literal, not a URL, a path, or a host:port pair.

    Catches the shapes an operator actually types by mistake — a ``https://``
    prefix, an appended ``:9000``, a filesystem path — each of which produces
    a certificate that verifies against nothing and fails at the handshake as
    a *trust* error.

    A colon is the awkward case: it is wrong in ``engine:9000`` and correct
    in an IPv6 literal. Rather than pattern-match the difference, the value
    is handed to ``ipaddress`` — if it parses as an address it is one, and if
    it does not, a colon means a port was appended.
    """
    if not value or value != value.strip():
        return False
    if any(token in value for token in ("/", "\\", " ", "\t")):
        return False
    if ":" in value:
        import ipaddress
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return False
    return True


class _Args:
    """Parsed ``ov link`` argv. Refuses what it does not recognise."""

    def __init__(self) -> None:
        self.mode: Optional[str] = None
        self.server_names: list = []
        self.client_names: list = []
        self.directory: Optional[Path] = None
        self.host: Optional[str] = None
        self.port: Optional[int] = None
        self.session: Optional[str] = None
        self.node: Optional[str] = None
        self.force = False
        self.error: str = ""


_MODES = {"--issue-certs": "issue", "--serve": "serve",
          "--connect": "connect", "--status": "status"}
_VALUED = {"--server-name", "--client-name", "--dir", "--host", "--port",
           "--session", "--node", "--connect"}
_KNOWN = set(_MODES) | _VALUED | {"--force", "--help", "-h"}


def parse(argv: Sequence[str]) -> _Args:
    """Pure argv → :class:`_Args`. Never raises, never reads the environment.

    Separated from execution so the routing is unit-testable without a socket
    or a filesystem — the same discipline ``ov.resolve`` follows.
    """
    args = _Args()
    tokens = list(argv)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--help", "-h"):
            args.mode = "help"
            return args
        if tok not in _KNOWN:
            hint = next((k for k in sorted(_KNOWN)
                         if k.startswith(tok[:6]) and k != tok), None)
            args.error = (f"unknown flag {tok!r}"
                          + (f" — did you mean {hint!r}?" if hint else ""))
            return args
        if tok in _MODES:
            if args.mode and args.mode != _MODES[tok]:
                args.error = (f"{tok} conflicts with the mode already given "
                              f"({args.mode}); pick one")
                return args
            args.mode = _MODES[tok]
            if tok == "--connect":
                if i + 1 >= len(tokens) or tokens[i + 1].startswith("-"):
                    args.error = "--connect requires a HOST"
                    return args
                args.host = tokens[i + 1]
                i += 2
                continue
            i += 1
            continue
        if tok == "--force":
            args.force = True
            i += 1
            continue
        if i + 1 >= len(tokens):
            args.error = f"{tok} requires a value"
            return args
        value = tokens[i + 1]
        if tok == "--server-name":
            args.server_names.append(value)
        elif tok == "--client-name":
            args.client_names.append(value)
        elif tok == "--dir":
            args.directory = Path(value).expanduser()
        elif tok == "--host":
            args.host = value
        elif tok == "--session":
            args.session = value
        elif tok == "--node":
            args.node = value
        elif tok == "--port":
            try:
                args.port = int(value)
            except (TypeError, ValueError):
                args.error = f"--port must be an integer, got {value!r}"
                return args
            if not (0 <= args.port <= 65535):
                args.error = f"--port out of range: {args.port}"
                return args
        i += 2
    return args


def _say(console: Any, text: str) -> None:
    try:
        console.print(text, markup=False, highlight=False)
    except Exception:  # noqa: BLE001
        print(text, flush=True)


def _build_loop(args: _Args) -> Any:
    """Construct the session loop from arguments and canonical defaults."""
    from backend.core.ouroboros.governance import link_session as ls
    session_id = (args.session or os.environ.get("JARVIS_LINK_SESSION")
                  or "ov-link")
    node = args.node or os.environ.get("JARVIS_LINK_NODE") or _hostname()
    spill_root = Path(os.environ.get("JARVIS_PROJECT_ROOT", ".")) / ".jarvis"
    return ls.LinkSessionLoop(
        ls.SessionConfig(
            node_id=node, session_id=session_id,
            spill_path=spill_root / "link_outbox.log",
        ),
        on_frame=lambda frame: logger.info(
            "[ov link] %s seq=%s", frame.get("kind"), frame.get("seq")),
    )


def _run_issue(console: Any, args: _Args) -> int:
    from backend.core.ouroboros.governance import link_certs as lc

    names = list(args.server_names)
    if not names:
        _say(console,
             "--issue-certs requires at least one --server-name.\n"
             "It must be a name the Body will dial the Engine by — its "
             "tailnet DNS name and/or its 100.x address. This cannot be "
             "guessed: a certificate issued for the wrong name fails the "
             "handshake as a trust error, which is far harder to diagnose "
             "than a missing argument.")
        return EX_USAGE
    bad = [n for n in names + args.client_names if not _looks_like_a_name(n)]
    if bad:
        _say(console,
             f"not usable as certificate names: {', '.join(repr(b) for b in bad)}\n"
             "Give a bare hostname or IP — no scheme, no port, no path.")
        return EX_USAGE

    try:
        issued = lc.issue_link_material(
            directory=args.directory, server_names=names,
            client_names=args.client_names or None, force=args.force)
    except lc.CertToolUnavailable as exc:
        _say(console, f"{exc}\n  pip install cryptography")
        return EX_UNAVAILABLE
    except FileExistsError as exc:
        _say(console, f"{exc}\n\nPass --force only if you can re-copy the CA "
                      f"to the other machine afterwards.")
        return EX_CONFIG
    except (ValueError, OSError) as exc:
        _say(console, f"could not issue material: {exc}")
        return EX_CONFIG

    _say(console, f"issued link material in {issued.directory}")
    _say(console, f"  server names : {', '.join(issued.server_names)}")
    _say(console, f"  client names : {', '.join(issued.client_names)}")
    _say(console, f"  valid until  : {issued.not_after}")
    _say(console, "")
    _say(console, "Copy EXACTLY these to the Body (the CA private key stays "
                  "here):")
    for name in lc.files_to_copy_to_peer():
        _say(console, f"  {issued.directory / name}")
    return 0


def _run_status(console: Any, args: _Args) -> int:
    from backend.core.ouroboros.governance import link_certs as lc
    report = lc.inspect_material(args.directory)
    _say(console, f"link material in {report['directory']}")
    certs = report.get("certificates") or {}
    if not certs:
        _say(console, "  none installed — run `ov link --issue-certs`")
        return EX_CONFIG
    worst = 10 ** 6
    for name, entry in sorted(certs.items()):
        if "error" in entry:
            _say(console, f"  {name}: unreadable — {entry['error']}")
            worst = -1
            continue
        days = entry["days_remaining"]
        worst = min(worst, days)
        flag = "EXPIRED" if entry["expired"] else f"{days}d remaining"
        _say(console, f"  {name}: {flag}")
        if entry.get("sans"):
            _say(console, f"      names: {', '.join(entry['sans'])}")
    if worst < 0:
        return EX_CONFIG
    if worst <= 0:
        _say(console, "\nMaterial has EXPIRED — re-issue and re-copy to the "
                      "peer.")
        return EX_CONFIG
    if worst < 30:
        _say(console, f"\nExpires in {worst} days — re-issue while you still "
                      f"have access to both machines.")
    return 0


def _run_serve(console: Any, args: _Args) -> int:
    from backend.core.ouroboros.governance import link_runner as lr

    loop = _build_loop(args)

    async def _main() -> int:
        try:
            server = await lr.serve_link(loop, host=args.host,
                                         port=args.port)
        except ConnectionError as exc:
            _say(console, str(exc))
            return EX_CONFIG
        bound = server.sockets[0].getsockname() if server.sockets else ("?", 0)
        _say(console, f"engine listening on {bound[0]}:{bound[1]}  "
                      f"session={loop.config.session_id} "
                      f"node={loop.config.node_id}")
        _say(console, "Ctrl+C to stop; the session parks and resumes on "
                      "reconnect.")
        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            pass
        return 0

    return _drive(console, _main())


def _run_connect(console: Any, args: _Args) -> int:
    from backend.core.ouroboros.governance import link_runner as lr

    if not args.host or not _looks_like_a_name(args.host):
        _say(console, f"--connect needs a bare host, got {args.host!r}. "
                      "It must match a name in the Engine's certificate.")
        return EX_USAGE
    port = args.port if args.port is not None else int(
        os.environ.get("JARVIS_LINK_PORT", "0") or 0)
    if not port:
        _say(console, "--connect requires --port (or JARVIS_LINK_PORT). The "
                      "Engine prints the port it bound.")
        return EX_USAGE

    loop = _build_loop(args)
    runner = lr.LinkRunner(loop, connector=lr.tls_connector(args.host, port))

    async def _main() -> int:
        _say(console, f"body dialing {args.host}:{port}  "
                      f"session={loop.config.session_id} "
                      f"node={loop.config.node_id}")
        _say(console, "Ctrl+C to stop; a drop parks rather than fails.")
        await runner.run()
        return 0

    return _drive(console, _main())


def _drive(console: Any, coro: Any) -> int:
    """Run a coroutine to completion, translating interruption cleanly.

    Ctrl+C is an ordinary way to stop a daemon, not a crash: it exits 130
    (128+SIGINT) with a line, never a KeyboardInterrupt traceback.
    """
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        _say(console, "\nstopped")
        return 130
    except ConnectionError as exc:
        _say(console, str(exc))
        return EX_CONFIG


def run_link(console: Any, argv: Optional[Sequence[str]] = None) -> int:
    """``ov link`` entry point. Returns a process exit code."""
    args = parse(list(argv or ()))
    if args.error:
        _say(console, args.error)
        _say(console, "")
        _say(console, LINK_HELP)
        return EX_USAGE
    if args.mode in (None, "help"):
        _say(console, LINK_HELP)
        return 0 if args.mode == "help" else EX_USAGE
    if args.mode == "issue":
        return _run_issue(console, args)
    if args.mode == "status":
        return _run_status(console, args)
    if args.mode == "serve":
        return _run_serve(console, args)
    if args.mode == "connect":
        return _run_connect(console, args)
    _say(console, LINK_HELP)
    return EX_USAGE
