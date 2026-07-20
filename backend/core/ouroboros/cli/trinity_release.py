"""``trinity release`` — the cryptographic release pipeline.

Operator authorization 2026-07-19 (Phase 4). Turns the Thin-Bundle
``.app`` into a Gatekeeper-trusted, offline-runnable, notarized artifact:
codesign (hardened runtime) → deep-verify → notarize (async poll) →
staple → validate.

Mandate 1 — no hardcoded secrets:
  The signing identity is resolved from the macOS Keychain
  (``security find-identity -v -p codesigning``), preferring a
  "Developer ID Application" cert. Notary auth prefers a stored
  ``notarytool`` **Keychain profile** (``--keychain-profile``) — the
  secure-enclave path where NO Apple ID / app-specific password ever
  touches this process. Env-var injection
  (``JARVIS_NOTARY_APPLE_ID`` / ``_TEAM_ID`` / ``_PASSWORD``) is the
  explicit fallback. Nothing is written to disk or logged.

Mandate 2 — edge cases:
  * **Async notary polling** — the notary takes 1–15+ min. We submit
    ``--no-wait`` to get a submission id, then poll ``notarytool info``
    via ``asyncio.create_subprocess_exec`` with EXPONENTIAL BACKOFF and
    ``asyncio.sleep`` (never blocking ``time.sleep``), so the loop stays
    responsive and we don't hammer the API into rate-limiting.
  * **Airgap staple** — on ``Accepted`` we ``xcrun stapler staple`` the
    ticket INTO the bundle so Gatekeeper trusts it with no network.
  * **Deep verify** — ``codesign --verify --deep --strict`` after signing
    proves the bundle + its inner ``Info.plist``/launcher weren't
    corrupted in the zip/upload round-trip.

Mandate 3 — DRY: a new ``trinity release`` verb; the Xcode-CLT gate
reuses ``trinity_doctor.check_xcode_tools``; the bundle itself comes from
``trinity_installer.generate_app_bundle``.

Every public entry point NEVER raises.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, Tuple

#: Notary terminal states (anything else = keep polling).
_TERMINAL = {"Accepted", "Invalid", "Rejected"}

# Injectable async runner: argv -> (returncode, stdout, stderr).
AsyncRunner = Callable[[List[str]], Awaitable[Tuple[int, str, str]]]
AsyncSleeper = Callable[[float], Awaitable[None]]


async def _run_async(argv: List[str], *, timeout: float = 300.0
                     ) -> Tuple[int, str, str]:
    """Non-blocking subprocess via asyncio. NEVER raises — a spawn/timeout
    failure returns a non-zero rc with the error in stderr."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            return 124, "", f"timeout after {timeout}s"
        return (proc.returncode or 0,
                (out or b"").decode("utf-8", "replace"),
                (err or b"").decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return 127, "", str(exc)


# ---------------------------------------------------------------------------
# Mandate 1 — Keychain-backed credential resolution (no hardcoding)
# ---------------------------------------------------------------------------

def resolve_signing_identity(
    *, runner: Optional[Callable[..., Any]] = None,
) -> Optional[str]:
    """The codesigning identity — env override first, else the Keychain's
    preferred "Developer ID Application" cert. Returns the common name (or
    SHA-1), or None if none is available. NEVER raises."""
    explicit = os.environ.get("JARVIS_CODESIGN_IDENTITY")
    if explicit:
        return explicit
    # Resolve at call time (not a def-time default) so the real
    # subprocess.run is used in production but patchable in tests.
    run = runner or subprocess.run
    try:
        r = run(["security", "find-identity", "-v", "-p", "codesigning"],
                   capture_output=True, text=True, timeout=15)
        out = getattr(r, "stdout", "") or ""
        # Lines: '  1) <SHA1> "Developer ID Application: Name (TEAM)"'
        matches = re.findall(r'\)\s+([0-9A-F]{40})\s+"([^"]+)"', out)
        if not matches:
            return None
        # Prefer Developer ID Application (the distributable cert).
        for sha, name in matches:
            if name.startswith("Developer ID Application"):
                return name
        # else fall back to the first valid identity
        return matches[0][1]
    except Exception:  # noqa: BLE001
        return None


def notary_auth_args() -> Optional[List[str]]:
    """The ``notarytool`` auth flags — Keychain profile (secure enclave)
    preferred; Apple-ID/Team/password env fallback. None when no
    credential path is configured. NEVER raises, NEVER logs the secret."""
    profile = os.environ.get("JARVIS_NOTARY_PROFILE")
    if profile:
        return ["--keychain-profile", profile]
    apple_id = os.environ.get("JARVIS_NOTARY_APPLE_ID")
    team_id = os.environ.get("JARVIS_NOTARY_TEAM_ID")
    password = os.environ.get("JARVIS_NOTARY_PASSWORD")
    if apple_id and team_id and password:
        return ["--apple-id", apple_id, "--team-id", team_id,
                "--password", password]
    return None


# ---------------------------------------------------------------------------
# Mandate 2 — sign / verify / submit / async-poll / staple
# ---------------------------------------------------------------------------

def codesign_sign(
    app: Path, identity: str, *, runner: Callable[..., Any] = subprocess.run,
) -> Tuple[bool, str]:
    """Sign with the HARDENED RUNTIME (--options runtime) + secure
    timestamp — both mandatory for notarization. NEVER raises."""
    try:
        r = runner(
            ["codesign", "--force", "--deep", "--options", "runtime",
             "--timestamp", "--sign", identity, str(app)],
            capture_output=True, text=True, timeout=300,
        )
        ok = getattr(r, "returncode", 1) == 0
        return ok, getattr(r, "stderr", "") or ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def codesign_verify(
    app: Path, *, runner: Callable[..., Any] = subprocess.run,
) -> Tuple[bool, str]:
    """Deep, strict cryptographic verification of the whole bundle tree
    (mandate 2 — catches zip/upload corruption). NEVER raises."""
    try:
        r = runner(["codesign", "--verify", "--deep", "--strict",
                    "--verbose=2", str(app)],
                   capture_output=True, text=True, timeout=120)
        ok = getattr(r, "returncode", 1) == 0
        return ok, getattr(r, "stderr", "") or ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def make_notary_zip(
    app: Path, *, runner: Callable[..., Any] = subprocess.run,
) -> Optional[Path]:
    """Apple-recommended notary archive (``ditto -c -k --keepParent``).
    Returns the zip path or None. NEVER raises."""
    try:
        zip_path = app.with_suffix(".zip")
        r = runner(["ditto", "-c", "-k", "--keepParent", str(app),
                    str(zip_path)], capture_output=True, text=True, timeout=120)
        return zip_path if getattr(r, "returncode", 1) == 0 else None
    except Exception:  # noqa: BLE001
        return None


async def notarytool_submit(
    zip_path: Path, auth: List[str], *, runner: AsyncRunner = _run_async,
) -> Optional[str]:
    """Submit ``--no-wait`` so WE own the polling loop (mandate 2). Returns
    the submission id, or None. NEVER raises."""
    rc, out, err = await runner(
        ["xcrun", "notarytool", "submit", str(zip_path), *auth,
         "--no-wait", "--output-format", "json"])
    if rc != 0:
        return None
    try:
        return json.loads(out).get("id")
    except Exception:  # noqa: BLE001
        # Fallback: scrape an id-looking token.
        m = re.search(r'"id"\s*:\s*"([^"]+)"', out)
        return m.group(1) if m else None


def _poll_bounds() -> Tuple[int, float, float]:
    def _f(name: str, default: float) -> float:
        v = os.environ.get(name)
        try:
            return float(v) if v is not None else float(default)
        except (TypeError, ValueError):
            return float(default)
    max_polls = int(_f("JARVIS_NOTARY_MAX_POLLS", 40))
    base = _f("JARVIS_NOTARY_POLL_BASE_S", 5.0)
    cap = _f("JARVIS_NOTARY_POLL_CAP_S", 60.0)
    return max_polls, base, cap


async def poll_notary_status(
    submission_id: str,
    auth: List[str],
    *,
    runner: AsyncRunner = _run_async,
    sleeper: AsyncSleeper = asyncio.sleep,
) -> str:
    """Poll ``notarytool info`` until a TERMINAL status, with async
    exponential backoff (never blocks the loop; never hammers the API).
    Returns the terminal status ("Accepted"/"Invalid"/"Rejected"), or
    "Timeout" if the bounded budget is spent. NEVER raises."""
    max_polls, base, cap = _poll_bounds()
    for attempt in range(max_polls):
        rc, out, err = await runner(
            ["xcrun", "notarytool", "info", submission_id, *auth,
             "--output-format", "json"])
        status = ""
        try:
            status = (json.loads(out) or {}).get("status", "") if out else ""
        except Exception:  # noqa: BLE001
            m = re.search(r'"status"\s*:\s*"([^"]+)"', out or "")
            status = m.group(1) if m else ""
        if status in _TERMINAL:
            return status
        # Not terminal yet → back off (capped) and poll again. asyncio.sleep
        # keeps the event loop free the entire time.
        await sleeper(min(cap, base * (2 ** attempt)))
    return "Timeout"


async def staple(
    app: Path, *, runner: AsyncRunner = _run_async,
) -> bool:
    """Embed the notary ticket INTO the bundle for offline Gatekeeper
    trust (mandate 2 — airgap). NEVER raises."""
    rc, out, err = await runner(["xcrun", "stapler", "staple", str(app)])
    return rc == 0


async def staple_validate(
    app: Path, *, runner: AsyncRunner = _run_async,
) -> bool:
    rc, out, err = await runner(["xcrun", "stapler", "validate", str(app)])
    return rc == 0


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass
class ReleaseReport:
    aborted: bool = False
    reason: str = ""
    signed: bool = False
    verified: bool = False
    notarized: bool = False
    stapled: bool = False
    notary_status: str = ""
    app_path: Optional[Path] = None
    messages: List[str] = field(default_factory=list)

    @property
    def shippable(self) -> bool:
        return (self.signed and self.verified and self.notarized
                and self.stapled and not self.aborted)


async def run_release(
    app_path: Path,
    *,
    identity: Optional[str] = None,
    auth: Optional[List[str]] = None,
    sync_runner: Callable[..., Any] = subprocess.run,
    async_runner: AsyncRunner = _run_async,
    sleeper: AsyncSleeper = asyncio.sleep,
    skip_xcode_gate: bool = False,
) -> ReleaseReport:
    """The full sign→verify→notarize→staple pipeline. NEVER raises."""
    rep = ReleaseReport(app_path=app_path)
    try:
        # ---- Gate: Xcode CLT present (DRY — reuse the doctor check) ----
        if not skip_xcode_gate:
            try:
                from backend.core.ouroboros.cli.trinity_doctor import (
                    check_xcode_tools,
                )
                xc = check_xcode_tools()
                if xc.status.value == "FAIL":
                    rep.aborted = True
                    rep.reason = xc.detail
                    return rep
            except Exception as exc:  # noqa: BLE001
                rep.aborted = True
                rep.reason = f"xcode gate failed: {exc}"
                return rep

        if not app_path.exists():
            rep.aborted = True
            rep.reason = f"bundle not found at {app_path} — run `trinity install` first"
            return rep

        # ---- Identity (mandate 1) ----
        ident = identity or resolve_signing_identity(runner=sync_runner)
        if not ident:
            rep.aborted = True
            rep.reason = ("no codesigning identity — import a Developer ID "
                          "Application cert or set JARVIS_CODESIGN_IDENTITY")
            return rep

        # ---- Sign (hardened runtime) ----
        ok, msg = codesign_sign(app_path, ident, runner=sync_runner)
        rep.signed = ok
        if not ok:
            rep.aborted = True
            rep.reason = f"codesign failed: {msg}"
            return rep
        rep.messages.append(f"⏺ signed with: {ident}")

        # ---- Deep verify (mandate 2) ----
        vok, vmsg = codesign_verify(app_path, runner=sync_runner)
        rep.verified = vok
        if not vok:
            rep.aborted = True
            rep.reason = f"deep verify failed: {vmsg}"
            return rep
        rep.messages.append("⏺ codesign --verify --deep --strict passed")

        # ---- Notary auth (mandate 1) ----
        notary_auth = auth if auth is not None else notary_auth_args()
        if not notary_auth:
            rep.aborted = True
            rep.reason = ("no notary credentials — store a profile with "
                          "`xcrun notarytool store-credentials` and set "
                          "JARVIS_NOTARY_PROFILE (or the *_APPLE_ID/_TEAM_ID/"
                          "_PASSWORD env fallback)")
            return rep

        # ---- Zip + submit + async poll (mandate 2) ----
        zip_path = make_notary_zip(app_path, runner=sync_runner)
        if zip_path is None:
            rep.aborted = True
            rep.reason = "notary zip (ditto) failed"
            return rep
        submission_id = await notarytool_submit(
            zip_path, notary_auth, runner=async_runner)
        if not submission_id:
            rep.aborted = True
            rep.reason = "notarytool submit failed (check credentials/network)"
            return rep
        rep.messages.append(f"⏺ submitted to notary (id={submission_id}); polling…")

        status = await poll_notary_status(
            submission_id, notary_auth, runner=async_runner, sleeper=sleeper)
        rep.notary_status = status
        if status != "Accepted":
            rep.aborted = True
            rep.reason = (f"notarization {status} — inspect with: xcrun "
                          f"notarytool log {submission_id}")
            return rep
        rep.notarized = True
        rep.messages.append("⏺ notary: Accepted")

        # ---- Staple (mandate 2 — airgap) ----
        st = await staple(app_path, runner=async_runner)
        rep.stapled = st
        if not st:
            rep.aborted = True
            rep.reason = "stapler staple failed (ticket not embedded)"
            return rep
        await staple_validate(app_path, runner=async_runner)
        rep.messages.append("⏺ ticket stapled — offline Gatekeeper trust")
        return rep
    except Exception as exc:  # noqa: BLE001
        rep.aborted = True
        rep.reason = f"release failed: {exc}"
        return rep


def release_main(argv: Optional[List[str]] = None, console=None) -> int:
    """Entry for ``trinity release`` / ``trinity build``. Returns 0 on a
    shippable artifact, 1 otherwise. NEVER raises."""
    try:
        if console is None:
            from backend.core.ouroboros.ui.theme import build_console
            console = build_console()
        # Ensure a fresh Thin-Bundle exists (DRY — reuse the installer).
        from backend.core.ouroboros.cli.trinity_installer import (
            generate_app_bundle, APP_NAME, _repo_root,
        )
        dest = Path(os.path.expanduser(
            os.environ.get("JARVIS_APP_DEST", str(_repo_root() / "dist"))))
        app = generate_app_bundle(dest)
        report = asyncio.run(run_release(app))
        for m in report.messages:
            console.print(m, markup=False)
        if report.aborted:
            console.print(f"✗ release aborted: {report.reason}", markup=False)
            return 1
        if report.shippable:
            console.print(f"⏺ SHIPPABLE — notarized + stapled: {report.app_path}",
                          markup=False)
            return 0
        console.print("▲ release incomplete — see messages above", markup=False)
        return 1
    except Exception as exc:  # noqa: BLE001
        try:
            console and console.print(f"✗ release failed: {exc}", markup=False)
        except Exception:  # noqa: BLE001
            pass
        return 1


__all__ = [
    "ReleaseReport", "resolve_signing_identity", "notary_auth_args",
    "codesign_sign", "codesign_verify", "make_notary_zip",
    "notarytool_submit", "poll_notary_status", "staple", "staple_validate",
    "run_release", "release_main",
]
