"""Bootstrap handshake — atomic 0600 tempfile carrying PSK + port + pid.

Aegis writes this payload exactly once at boot. The harness reads it
exactly once, then unlinks it. No forensic trace of the PSK remains on
disk after handshake completion.

Payload fields (per operator's binding directives):

  * ``aegis_url``      — ``http://127.0.0.1:<ephemeral_port>``
  * ``bootstrap_psk``  — high-entropy single-use string (``secrets.token_urlsafe``)
  * ``daemon_pid``     — Aegis subprocess PID (for harness lifecycle)
  * ``expires_at``     — unix timestamp; harness rejects payload past
                         this point (defense against stale-file replay)
  * ``schema_version`` — bumpable additive contract

Atomic write protocol: write the full payload to a sibling temp file
(``<final>.tmp.<random>``), fsync + close, then ``os.rename`` onto the
final path. POSIX guarantees ``rename`` is atomic *and* the final path
is only visible to other processes AFTER the rename completes. This
closes the read-during-write race the harness's poll loop would
otherwise hit if we used ``O_CREAT`` directly on the final path
(create happens before write — observable empty file).

The temp file is created with ``O_CREAT|O_EXCL|0o600`` so the same
discipline (atomic, owner-only) still holds, and the random suffix
prevents collisions between concurrent boots in the same dir.

Stdlib only. No async — this is one-shot file I/O at boot, sub-millisecond.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


BOOTSTRAP_PAYLOAD_SCHEMA_VERSION: str = "aegis_bootstrap.1"

# Tempfile mode: owner read/write only. Anyone else (incl. another
# user on the same machine) cannot read the PSK.
_PAYLOAD_FILE_MODE: int = 0o600

# Bootstrap PSK entropy: 48 bytes -> ~64 url-safe chars -> 384 bits.
# Comfortably above the operator's binding "high-entropy" bar.
_PSK_ENTROPY_BYTES: int = 48

# Default expiry window: 60s. Long enough for harness to read +
# unlink even on a busy machine; short enough that a stale payload
# discovered later cannot be replayed.
_DEFAULT_EXPIRY_WINDOW_S: int = 60


@dataclass(frozen=True)
class BootstrapPayload:
    """Frozen single-use handshake artifact written by Aegis, read by harness.

    Equality + hash are content-based (frozen dataclass). ``to_dict`` /
    ``from_dict`` are §33.5 lossless roundtrip.
    """

    aegis_url: str
    bootstrap_psk: str
    daemon_pid: int
    expires_at: float
    schema_version: str = BOOTSTRAP_PAYLOAD_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "aegis_url": self.aegis_url,
            "bootstrap_psk": self.bootstrap_psk,
            "daemon_pid": self.daemon_pid,
            "expires_at": self.expires_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "BootstrapPayload":
        return cls(
            aegis_url=str(raw["aegis_url"]),
            bootstrap_psk=str(raw["bootstrap_psk"]),
            daemon_pid=int(raw["daemon_pid"]),
            expires_at=float(raw["expires_at"]),
            schema_version=str(raw.get(
                "schema_version", BOOTSTRAP_PAYLOAD_SCHEMA_VERSION,
            )),
        )


def mint_bootstrap_psk() -> str:
    """Return a fresh high-entropy bootstrap PSK.

    URL-safe so the harness can transport it via env var without
    escaping. ~384 bits of entropy — significantly higher than the
    HMAC key the daemon protects (256 bits) because the PSK is the
    only thing standing between any localhost process and a session
    token until /session/establish is called.
    """
    return secrets.token_urlsafe(_PSK_ENTROPY_BYTES)


def default_expiry(*, now_s: float, window_s: int = _DEFAULT_EXPIRY_WINDOW_S) -> float:
    """Compute the absolute expiry timestamp from ``now``.

    Caller supplies ``now_s`` (typically ``time.time()``) so tests can
    inject deterministic clocks.
    """
    return now_s + float(window_s)


def atomic_write_payload(payload: BootstrapPayload, path: Path) -> None:
    """Atomically write ``payload`` to ``path`` with mode 0o600.

    Implementation: write the full payload to a sibling temp file
    (``<path>.tmp.<random>``) with ``O_CREAT|O_EXCL|O_WRONLY`` + 0o600,
    fsync + close, then ``os.rename`` onto the final path. POSIX
    ``rename`` is atomic AND the final path becomes visible to other
    processes only AFTER the rename — so a poller cannot observe the
    file in an empty/partial state.

    Parent directory is created with mode 0o700 if missing.

    Raises:
        OSError: on any filesystem failure (permission denied, disk
            full, etc.). Caller treats as session-fatal.
        FileExistsError: if ``path`` already exists at the moment of
            rename. Aegis refuses to overwrite — the harness must
            always supply a fresh path.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # On most systems mkdir respects the requested mode only if the
    # parent didn't already exist; tighten existing parent dir to 0o700
    # if it was looser.
    try:
        st = target.parent.stat()
        if (st.st_mode & 0o777) != 0o700:
            os.chmod(target.parent, 0o700)
    except OSError:
        pass

    if target.exists():
        # Mirror the previous O_EXCL semantic: the harness contract is
        # "fresh path per boot." A pre-existing target is a hard error.
        raise FileExistsError(
            f"refusing to overwrite existing bootstrap payload: {target}"
        )

    # Temp file in the same directory so rename is intra-filesystem.
    tmp_path = target.parent / f"{target.name}.tmp.{secrets.token_hex(8)}"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(tmp_path), flags, _PAYLOAD_FILE_MODE)
    try:
        data = json.dumps(payload.to_dict(), separators=(",", ":")).encode("utf-8")
        written = 0
        view = memoryview(data)
        while written < len(data):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError("short write while atomic-writing bootstrap payload")
            written += n
        os.fsync(fd)
    finally:
        os.close(fd)

    # Belt and suspenders: temp file mode pre-rename.
    try:
        current_mode = stat.S_IMODE(tmp_path.stat().st_mode)
        if current_mode != _PAYLOAD_FILE_MODE:
            os.chmod(tmp_path, _PAYLOAD_FILE_MODE)
    except OSError:
        pass

    # Atomic flip — the final path becomes visible to readers as a
    # fully-written file in one syscall.
    try:
        os.rename(str(tmp_path), str(target))
    except OSError:
        # Best-effort cleanup of the temp file if rename failed.
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise

    # Final mode confirmation post-rename (POSIX preserves mode across
    # rename, but tighten if any filesystem layer drifted).
    try:
        current_mode = stat.S_IMODE(target.stat().st_mode)
        if current_mode != _PAYLOAD_FILE_MODE:
            os.chmod(target, _PAYLOAD_FILE_MODE)
    except OSError:
        pass


def read_and_unlink_payload(path: Path) -> BootstrapPayload:
    """Read ``path``, unlink it, and return the parsed payload.

    The unlink happens AFTER the read returns successfully — if the
    read fails (parse error, missing field, etc.) the file is left in
    place so the operator can inspect what went wrong, but Aegis-side
    expiry will still invalidate it shortly.

    Raises:
        FileNotFoundError: payload file missing (Aegis never wrote, or
            another process already consumed it).
        ValueError: JSON parse failure or missing required field.
        OSError: filesystem error during read.
    """
    target = Path(path)
    raw_bytes = target.read_bytes()
    try:
        raw_obj = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"bootstrap payload at {target} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(raw_obj, dict):
        raise ValueError(
            f"bootstrap payload at {target} is not a JSON object"
        )
    try:
        payload = BootstrapPayload.from_dict(raw_obj)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"bootstrap payload at {target} missing/malformed field: {exc}"
        ) from exc

    # Unlink only after successful parse. Forensic-trace-free per
    # operator directive.
    try:
        target.unlink()
    except OSError:
        # If unlink fails the payload is still single-use because
        # the Aegis-side PSK ledger only honors it once.
        pass

    return payload


__all__ = [
    "BOOTSTRAP_PAYLOAD_SCHEMA_VERSION",
    "BootstrapPayload",
    "atomic_write_payload",
    "default_expiry",
    "mint_bootstrap_psk",
    "read_and_unlink_payload",
]
