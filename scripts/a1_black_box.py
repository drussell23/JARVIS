#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A1 Black Box Flight Recorder -- the node-side diagnostic bundler.

================================================================================
The absolute data-preservation guard for the A1 Live-Fire Chaos Harness: when a
remote GCP soak FAILS, this script FREEZES then DUMPS the entire diagnostic
context into a single compressed, cryptographically-checksummed archive on the
NODE -- BEFORE the node's dead-man is allowed to self-delete.

The orchestrator (``a1_live_fire_chaos_harness.py``) then PULLS this archive over
the IAP SSH tunnel, recomputes the sha256 on the LOCAL Mac, and ONLY authorizes
the node's self-delete when the local checksum MATCHES. The node can NEVER burn
diagnostic data before it is confirmed safely received locally.

THE INVARIANT (fail-CLOSED toward DATA PRESERVATION): this script's job is to
make a COMPLETE, VERIFIABLE bundle. It captures everything present, NOTES
everything absent (never aborts on a missing artifact), and emits a sha256 the
orchestrator verifies. When in doubt, capture MORE, never less.

What it captures (each bounded + fail-soft -- a missing artifact is noted in the
in-archive ``MANIFEST.txt``, never an abort):

  * OUROBOROS GLOBAL CONTEXT -- the session ``debug.log`` + ``summary.json`` + the
    whole ``.ouroboros/sessions/<id>/`` dir + the ``.jarvis/`` ledgers (intake,
    graduation, decision-trace, op-ledger).
  * AGENT-MESSAGE-BUS TRANSCRIPTS -- any swarm per-graph JSON message logs (may be
    empty if swarm off -- capture what exists, do not fail if absent).
  * DAG TOPOLOGY -- the autonomy ``execution_graph_store`` artifacts + the current
    op ledger.
  * AST DIFF -- the chaos manifest (``.jarvis/chaos_manifest.json``) + a ``git
    diff`` of the target file (original -> mutated -> O+V's-current-state) so we
    see exactly what O+V did (or did not do) to the injected bug.
  * PROVIDER ROUTING TELEMETRY (DW-primary audit) -- which provider served each
    generation (grepped from the debug.log + the dw_surface_health +
    provider_quarantine state) plus the resolved ``JARVIS_DW_PRIMARY_OVERRIDE`` +
    ``JARVIS_PROVIDER_CLAUDE_DISABLED`` values, so a DW throughput-collapse or an
    (should-be-NONE) Claude-fallback is visible.

Output: ``black_box_<run_id>.tar.gz`` + ``black_box_<run_id>.tar.gz.sha256`` in
``--out``. The archive path + sha256 are printed to stdout as structured
``BLACK_BOX_ARCHIVE=`` / ``BLACK_BOX_SHA256=`` lines the orchestrator parses.

Design: ``from __future__ import annotations``, Python 3.9+, ASCII-only, pure
stdlib, env-knob driven, no org mutation (read-only over the repo + ledgers).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


# ===========================================================================
# Paths + defaults (every value env-overridable -- no hardcoding).
# ===========================================================================

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT_DEFAULT = os.path.dirname(_SCRIPTS_DIR)

# The .jarvis ledgers we always try to capture (relative to repo_root/.jarvis).
# Env-overridable comma list so the capture set is tunable, not a frozen literal.
_LEDGER_FILES = [
    f.strip() for f in os.environ.get(
        "JARVIS_A1_BLACKBOX_LEDGERS",
        "intake_dlq.jsonl,graduation_ledger.jsonl,decision_trace.jsonl,"
        "op_ledger.jsonl,intake_router.lock,posture_current.jsonl,"
        "dw_surface_health.json,provider_quarantine.json,chaos_manifest.json",
    ).split(",") if f.strip()
]

# The .jarvis subdirs we capture wholesale (bounded by _MAX_DIR_BYTES).
_LEDGER_DIRS = [
    d.strip() for d in os.environ.get(
        "JARVIS_A1_BLACKBOX_LEDGER_DIRS",
        "agent_message_bus,execution_graph_store,autonomy,user_preferences",
    ).split(",") if d.strip()
]

# Per-file capture ceiling (bytes) -- keep the bundle bounded. Default 64 MiB.
_MAX_FILE_BYTES = int(os.environ.get("JARVIS_A1_BLACKBOX_MAX_FILE_BYTES", str(64 * 1024 * 1024)))
# Per-dir capture ceiling (bytes) -- bounded wholesale dir capture. Default 128 MiB.
_MAX_DIR_BYTES = int(os.environ.get("JARVIS_A1_BLACKBOX_MAX_DIR_BYTES", str(128 * 1024 * 1024)))
# debug.log tail line cap when the file is huge (keep the recent failure window).
_DEBUG_LOG_TAIL_LINES = int(os.environ.get("JARVIS_A1_BLACKBOX_DEBUG_TAIL_LINES", "20000"))

# Provider-telemetry grep tokens (env-overridable, comma list).
_PROVIDER_TOKENS = [
    t.strip() for t in os.environ.get(
        "JARVIS_A1_BLACKBOX_PROVIDER_TOKENS",
        "served_by,[provider],DWSurface,provider_quarantine,SOVEREIGN YIELD,"
        "doubleword,gpt-oss,claude,fallback,terminal_quota,autarky",
    ).split(",") if t.strip()
]


def _log(msg: str) -> None:
    print("[BlackBox] %s" % (msg,), file=sys.stderr, flush=True)


# ===========================================================================
# Config + result dataclasses.
# ===========================================================================


@dataclass
class BundleConfig:
    """All inputs the bundler needs. ``git_diff_runner`` + ``env`` are injectable
    so the test feeds deterministic values (no real git / no real process env)."""

    run_id: str
    repo_root: str = _REPO_ROOT_DEFAULT
    out_dir: str = "."
    session_dir: str = ""  # the .ouroboros/sessions/<id> dir (auto-discovered if "")
    git_diff_runner: Optional[Callable[[str], str]] = None
    env: Optional[Dict[str, str]] = None


@dataclass
class BundleResult:
    archive_path: str
    sha256_path: str
    sha256: str
    captured_count: int
    absent_count: int
    captured: List[str] = field(default_factory=list)
    absent: List[str] = field(default_factory=list)


# ===========================================================================
# Discovery helpers.
# ===========================================================================


def _discover_session_dir(repo_root: str) -> str:
    """Best-effort newest ``.ouroboros/sessions/bt-*`` dir. Fail-soft ('' )."""
    root = os.path.join(repo_root, ".ouroboros", "sessions")
    try:
        names = [n for n in os.listdir(root) if n != "pending"]
    except OSError:
        return ""
    cand: List[Tuple[float, str]] = []
    for n in names:
        p = os.path.join(root, n)
        try:
            cand.append((os.path.getmtime(p), p))
        except OSError:
            continue
    if not cand:
        return ""
    cand.sort(reverse=True)
    return cand[0][1]


def _chaos_manifest_path(repo_root: str) -> str:
    return os.path.join(repo_root, ".jarvis", "chaos_manifest.json")


def _read_text_bounded(path: str, *, max_bytes: int = _MAX_FILE_BYTES) -> Optional[str]:
    """Read a text file fail-soft, bounded. None on absence/error."""
    try:
        if not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            if size > max_bytes:
                # Keep the tail (the recent failure window).
                return fh.read()[-max_bytes:]
            return fh.read()
    except OSError:
        return None


def _git_diff_of_target(repo_root: str, target_rel: str) -> str:
    """Real ``git diff`` of the chaos target file (original -> current). Fail-soft
    -- returns a note string on any error (never raises). The orchestrator and the
    tests inject a stub runner; only the production main() path reaches here."""
    if not target_rel:
        return "[no chaos target_file -- no git diff captured]"
    try:
        cp = subprocess.run(
            ["git", "-C", repo_root, "diff", "--", target_rel],
            capture_output=True, text=True, timeout=60.0, check=False,
        )
        out = (cp.stdout or "") + (cp.stderr or "")
        return out if out.strip() else "[git diff empty -- target unchanged on disk]"
    except Exception as exc:  # noqa: BLE001 -- capture must never abort the bundle
        return "[git diff failed: %r]" % (exc,)


# ===========================================================================
# Section builders -- each returns the text body of an in-archive section.
# ===========================================================================


def _build_ast_diff_section(cfg: BundleConfig) -> Tuple[str, Optional[str]]:
    """The AST-diff section: chaos manifest summary + git diff of the target.

    Returns (ast_diff_text, manifest_json_or_None). The git diff shows exactly
    what O+V did (or did not do) to the injected bug."""
    man_path = _chaos_manifest_path(cfg.repo_root)
    man_text = _read_text_bounded(man_path)
    target_rel = ""
    parts: List[str] = ["=== A1 BLACK BOX -- AST DIFF (what O+V did to the injected bug) ==="]
    if man_text is not None:
        try:
            man = json.loads(man_text)
            target_rel = str(man.get("target_file") or "")
            parts.append("chaos_target_file : %s" % (target_rel or "<unknown>"))
            parts.append("chaos_function    : %s" % (man.get("function") or "<unknown>"))
            parts.append("chaos_test_node   : %s" % (man.get("test_node") or "<unknown>"))
            parts.append("chaos_active      : %s" % (man.get("active")))
        except Exception:  # noqa: BLE001
            parts.append("[chaos_manifest.json present but unparseable]")
    else:
        parts.append("[chaos_manifest.json ABSENT -- no AST mutation recorded]")

    # The git diff of the target file (original -> mutated -> O+V's-current-state).
    runner = cfg.git_diff_runner or (lambda t: _git_diff_of_target(cfg.repo_root, t))
    parts.append("")
    parts.append("--- git diff of target (%s) ---" % (target_rel or "<no target>"))
    try:
        parts.append(runner(target_rel))
    except Exception as exc:  # noqa: BLE001 -- diff capture must never abort
        parts.append("[git diff runner failed: %r]" % (exc,))
    return "\n".join(parts) + "\n", man_text


def _build_provider_telemetry_section(cfg: BundleConfig, debug_log: Optional[str]) -> str:
    """The DW-primary audit: which provider served each generation + the resolved
    DW-primary / Claude-disabled env values + the dw_surface_health +
    provider_quarantine snapshots. A Claude-fallback (should be NONE) or a DW
    throughput-collapse is visible here."""
    env = cfg.env if cfg.env is not None else dict(os.environ)
    parts: List[str] = ["=== A1 BLACK BOX -- PROVIDER ROUTING TELEMETRY (DW-primary audit) ==="]
    # The resolved DW-primary pins -- the load-bearing assertion values.
    parts.append("JARVIS_DW_PRIMARY_OVERRIDE     : %s" % (env.get("JARVIS_DW_PRIMARY_OVERRIDE", "<unset>")))
    parts.append("JARVIS_PROVIDER_CLAUDE_DISABLED: %s" % (env.get("JARVIS_PROVIDER_CLAUDE_DISABLED", "<unset>")))
    parts.append("JARVIS_PROVIDER_QUOTA_ISOLATION_ENABLED: %s"
                 % (env.get("JARVIS_PROVIDER_QUOTA_ISOLATION_ENABLED", "<unset>")))
    parts.append("")

    # State snapshots.
    for fname in ("dw_surface_health.json", "provider_quarantine.json"):
        snap = _read_text_bounded(os.path.join(cfg.repo_root, ".jarvis", fname))
        parts.append("--- %s ---" % (fname,))
        parts.append(snap if snap is not None else "[%s ABSENT]" % (fname,))
        parts.append("")

    # Grep the debug.log for provider-routing lines (which provider served each gen).
    parts.append("--- provider-routing lines (grepped from session debug.log) ---")
    if debug_log:
        hits: List[str] = []
        for line in debug_log.splitlines():
            low = line.lower()
            if any(tok.lower() in low for tok in _PROVIDER_TOKENS):
                hits.append(line)
        if hits:
            # Bound the captured hits so the section stays sane.
            parts.extend(hits[-2000:])
        else:
            parts.append("[no provider-routing lines matched in the debug.log]")
    else:
        parts.append("[session debug.log ABSENT -- no provider lines to grep]")
    return "\n".join(parts) + "\n"


# ===========================================================================
# tar assembly helpers (bounded + fail-soft per-artifact).
# ===========================================================================


def _add_text(tf: tarfile.TarFile, arcname: str, text: str) -> None:
    """Add an in-memory text blob to the tar (deterministic, no real file)."""
    data = text.encode("utf-8", errors="replace")
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o644
    tf.addfile(info, io.BytesIO(data))


def _add_file(tf: tarfile.TarFile, abs_path: str, arcname: str) -> bool:
    """Add a real file to the tar bounded by _MAX_FILE_BYTES. Returns True iff
    captured. Fail-soft -- any error returns False (the caller notes it absent)."""
    try:
        if not os.path.isfile(abs_path):
            return False
        size = os.path.getsize(abs_path)
        if size > _MAX_FILE_BYTES:
            # Capture only the tail (bounded) as a text blob.
            txt = _read_text_bounded(abs_path)
            if txt is None:
                return False
            _add_text(tf, arcname + ".tail", txt)
            return True
        tf.add(abs_path, arcname=arcname, recursive=False)
        return True
    except Exception as exc:  # noqa: BLE001 -- a per-file failure never aborts the bundle
        _log("file capture warning %s: %r" % (abs_path, exc))
        return False


def _add_dir(tf: tarfile.TarFile, abs_dir: str, arcname: str) -> int:
    """Add a directory tree to the tar, bounded by _MAX_DIR_BYTES total. Returns
    the number of files captured (0 == absent/empty). Fail-soft."""
    if not os.path.isdir(abs_dir):
        return 0
    captured = 0
    total = 0
    try:
        for root, _dirs, files in os.walk(abs_dir):
            for name in sorted(files):
                fp = os.path.join(root, name)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    continue
                if total + sz > _MAX_DIR_BYTES:
                    _log("dir capture cap hit at %s (%d bytes)" % (abs_dir, total))
                    return captured
                rel = os.path.relpath(fp, abs_dir)
                if _add_file(tf, fp, os.path.join(arcname, rel)):
                    captured += 1
                    total += sz
    except Exception as exc:  # noqa: BLE001
        _log("dir capture warning %s: %r" % (abs_dir, exc))
    return captured


# ===========================================================================
# THE bundler.
# ===========================================================================


def bundle(cfg: BundleConfig) -> BundleResult:
    """FREEZE then DUMP the full diagnostic context into a compressed archive +
    emit its sha256. Bounded + fail-soft per-artifact (a missing artifact is noted
    in MANIFEST.txt, never an abort). Returns the BundleResult with the archive
    path + the sha256 of the archive."""
    os.makedirs(cfg.out_dir, exist_ok=True)
    archive_path = os.path.join(cfg.out_dir, "black_box_%s.tar.gz" % (cfg.run_id,))
    sha_path = archive_path + ".sha256"

    session_dir = cfg.session_dir or _discover_session_dir(cfg.repo_root)
    captured: List[str] = []
    absent: List[str] = []

    def _note(label: str, ok: bool) -> None:
        (captured if ok else absent).append(label)

    # Pre-read the session debug.log text (also feeds the provider-telemetry grep).
    debug_log_path = os.path.join(session_dir, "debug.log") if session_dir else ""
    debug_log_text = _read_text_bounded(debug_log_path) if debug_log_path else None

    with tarfile.open(archive_path, "w:gz") as tf:
        # 1. OUROBOROS GLOBAL CONTEXT: the whole session dir + debug.log + summary.
        if session_dir and os.path.isdir(session_dir):
            n = _add_dir(tf, session_dir, "ouroboros_session")
            _note("ouroboros_session_dir", n > 0)
            _note("debug.log", os.path.isfile(debug_log_path))
            _note("summary.json", os.path.isfile(os.path.join(session_dir, "summary.json")))
        else:
            _note("ouroboros_session_dir", False)
            _note("debug.log", False)
            _note("summary.json", False)

        # 2. .jarvis LEDGERS (intake, graduation, decision-trace, op-ledger, ...).
        jdir = os.path.join(cfg.repo_root, ".jarvis")
        for fname in _LEDGER_FILES:
            ok = _add_file(tf, os.path.join(jdir, fname), os.path.join("jarvis_ledgers", fname))
            _note("ledger:%s" % (fname,), ok)

        # 3. AGENT-MESSAGE-BUS + DAG TOPOLOGY (+ other .jarvis subdirs).
        for dname in _LEDGER_DIRS:
            n = _add_dir(tf, os.path.join(jdir, dname), os.path.join("jarvis_dirs", dname))
            _note("dir:%s" % (dname,), n > 0)

        # 4. AST DIFF (chaos manifest + git diff of the target file).
        ast_text, man_text = _build_ast_diff_section(cfg)
        _add_text(tf, "ast_diff.txt", ast_text)
        captured.append("ast_diff.txt")
        if man_text is not None:
            _add_text(tf, "chaos_manifest.json", man_text)
            captured.append("chaos_manifest.json")
        else:
            absent.append("chaos_manifest.json")

        # 5. PROVIDER ROUTING TELEMETRY (DW-primary audit).
        prov_text = _build_provider_telemetry_section(cfg, debug_log_text)
        _add_text(tf, "provider_telemetry.txt", prov_text)
        captured.append("provider_telemetry.txt")

        # 6. MANIFEST.txt: what was captured + what was absent (loud, never silent).
        manifest_lines = [
            "A1 BLACK BOX FLIGHT RECORDER -- MANIFEST",
            "run_id     : %s" % (cfg.run_id,),
            "repo_root  : %s" % (cfg.repo_root,),
            "session_dir: %s" % (session_dir or "<not discovered>"),
            "stamped_at : %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            "",
            "CAPTURED (%d):" % (len(captured),),
        ]
        manifest_lines.extend("  + %s" % (c,) for c in captured)
        manifest_lines.append("")
        manifest_lines.append("ABSENT (%d) [noted, NOT a failure -- bundle still complete]:" % (len(absent),))
        manifest_lines.extend("  - %s" % (a,) for a in absent)
        manifest_lines.append("")
        _add_text(tf, "MANIFEST.txt", "\n".join(manifest_lines) + "\n")

    # Emit the sha256 OF THE ARCHIVE (the orchestrator verifies this locally).
    digest = hashlib.sha256()
    with open(archive_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    sha = digest.hexdigest()
    with open(sha_path, "w", encoding="utf-8") as fh:
        fh.write("%s  %s\n" % (sha, os.path.basename(archive_path)))

    return BundleResult(
        archive_path=archive_path, sha256_path=sha_path, sha256=sha,
        captured_count=len(captured), absent_count=len(absent),
        captured=captured, absent=absent,
    )


# ===========================================================================
# CLI
# ===========================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="a1_black_box.py",
        description=(
            "A1 Black Box Flight Recorder -- node-side diagnostic bundler. On a "
            "failed A1 soak, FREEZE+DUMP the full Ouroboros context + bus + DAG + "
            "AST-diff + provider-telemetry into black_box_<run_id>.tar.gz and emit "
            "its sha256. The orchestrator PULLs this over IAP, verifies the "
            "checksum LOCALLY, and only then authorizes the node self-delete -- the "
            "node never burns diagnostic data before confirmed local receipt."
        ),
    )
    p.add_argument("--bundle", action="store_true",
                   help="Build the black-box archive + sha256 and print their paths.")
    p.add_argument("--run-id", required=False, default="",
                   help="The run id (names the archive black_box_<run_id>.tar.gz).")
    p.add_argument("--out", required=False, default=".",
                   help="Output directory for the archive + .sha256 sidecar.")
    p.add_argument("--repo-root", default=_REPO_ROOT_DEFAULT,
                   help="Repo root (where .ouroboros + .jarvis live).")
    p.add_argument("--session-dir", default="",
                   help="The .ouroboros/sessions/<id> dir (auto-discovered if omitted).")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.bundle:
        _log("no action -- pass --bundle (see --help).")
        return 0
    run_id = args.run_id or time.strftime("a1-%Y%m%d-%H%M%S")

    # Resolve the chaos target for the git diff (real git -- module-level so the
    # test can monkeypatch _git_diff_of_target).
    man_path = _chaos_manifest_path(args.repo_root)
    target_rel = ""
    man_text = _read_text_bounded(man_path)
    if man_text is not None:
        try:
            target_rel = str(json.loads(man_text).get("target_file") or "")
        except Exception:  # noqa: BLE001
            target_rel = ""

    cfg = BundleConfig(
        run_id=run_id,
        repo_root=args.repo_root,
        out_dir=args.out,
        session_dir=args.session_dir,
        git_diff_runner=lambda _t: _git_diff_of_target(args.repo_root, target_rel),
        env=dict(os.environ),
    )
    try:
        result = bundle(cfg)
    except Exception as exc:  # noqa: BLE001 -- a bundle failure must be loud, never silent
        _log("FATAL bundle error: %r" % (exc,))
        # Fail-CLOSED toward preservation: a non-zero rc tells the orchestrator the
        # archive is NOT trustworthy -> it must HOLD the node, not burn.
        return 1

    # Structured stdout the orchestrator parses (path + sha256).
    print("BLACK_BOX_ARCHIVE=%s" % (os.path.abspath(result.archive_path),), flush=True)
    print("BLACK_BOX_SHA256=%s" % (result.sha256,), flush=True)
    _log("bundled %d artifact(s), %d absent -> %s (sha256=%s)"
         % (result.captured_count, result.absent_count, result.archive_path, result.sha256))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
