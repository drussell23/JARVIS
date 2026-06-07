"""Slice 125 — Safe provider-credential env bootstrap (pre-Aegis-snapshot).

Root invariant (operator mandate): BEFORE Aegis snapshots/confiscates provider
credentials, the process environment must already contain the funded provider
keys from the operator-approved ``.env``. Otherwise the Aegis daemon is spawned
with an empty/absent key, injects nothing, and the upstream provider bills the
request against its free ($0) tier → a misleading ``402 "balance too low"`` that
looks like an out-of-credits problem but is really a credential-injection gap.

This module loads ONLY an allowlist of provider-credential names from ``.env``
into the environment, with these guarantees:

  • **No shell execution.** ``.env`` is PARSED line-by-line (``KEY=VALUE``), never
    ``source``-d — a malicious/garbled ``.env`` cannot run arbitrary shell.
  • **Explicit env wins.** A name already present in the environment (an operator
    export) is NEVER overwritten — ``.env`` only fills gaps.
  • **Allowlist only.** Only known credential names are loaded; everything else
    in ``.env`` is ignored (no broad env injection).
  • **No secret leakage.** Values are never logged/printed; the report carries
    only a redacted fingerprint (``sha256(value)[:8]``) + the name.
  • **Stdlib only.** Importable at the very top of the harness entrypoint,
    BEFORE any ``backend.*`` provider module (which read keys at import time) and
    before the Aegis preflight snapshot.

Default-safe: a missing ``.env`` is a no-op (not an error) — explicit exports or
a real keyless environment are legitimate. A malformed ``.env`` yields a clear,
secret-free diagnostic.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
from typing import Dict, List, MutableMapping, Optional, Sequence

# The closed allowlist of provider-credential env names the soak needs. Keep this
# minimal — only credentials, never arbitrary config. (DoubleWord is the cheap
# primary; Anthropic is the fallback; HF for dataset pulls.)
PROVIDER_CREDENTIAL_ALLOWLIST = (
    "DOUBLEWORD_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
    "HUGGINGFACE_TOKEN",
)


def fingerprint(value: str) -> str:
    """Redacted, non-reversible identity for a secret — for logs/telemetry.
    NEVER returns any substring of the raw value."""
    if not value:
        return "sha256:<empty>"
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


@dataclasses.dataclass(frozen=True)
class CredentialLoadReport:
    loaded: Dict[str, str]            # name → fingerprint (filled from .env)
    already_present: List[str]        # names left untouched (explicit env wins)
    dotenv_present: bool
    error: str = ""                   # secret-free diagnostic, "" if clean

    @property
    def ok(self) -> bool:
        return not self.error


def parse_dotenv(text: str) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` lines safely (NO shell execution). Ignores blanks,
    comments, and ``export `` prefixes; strips matched surrounding quotes. A line
    without ``=`` or with an empty/invalid key is skipped (not fatal)."""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or not all(c.isalnum() or c == "_" for c in key):
            continue
        val = val.strip()
        # Strip a single matched pair of surrounding quotes (no shell semantics).
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def load_provider_credentials(
    *,
    allowlist: Sequence[str] = PROVIDER_CREDENTIAL_ALLOWLIST,
    dotenv_path: str = ".env",
    env: Optional[MutableMapping[str, str]] = None,
) -> CredentialLoadReport:
    """Fill allowlisted provider credentials from ``.env`` into ``env`` (default
    ``os.environ``), without overwriting explicit values. Never raises; never
    logs secrets. Returns a redacted report."""
    target = env if env is not None else os.environ
    loaded: Dict[str, str] = {}
    already: List[str] = []

    # Names already present (explicit export) win — record + skip.
    for name in allowlist:
        if target.get(name):
            already.append(name)

    if not os.path.exists(dotenv_path):
        return CredentialLoadReport(loaded={}, already_present=already, dotenv_present=False)

    try:
        with open(dotenv_path, "r", encoding="utf-8") as fh:
            parsed = parse_dotenv(fh.read())
    except OSError as exc:
        return CredentialLoadReport(
            loaded={}, already_present=already, dotenv_present=True,
            error=f"dotenv_unreadable: {exc.__class__.__name__}",
        )
    except Exception as exc:  # noqa: BLE001 - never leak, never raise
        return CredentialLoadReport(
            loaded={}, already_present=already, dotenv_present=True,
            error=f"dotenv_parse_error: {exc.__class__.__name__}",
        )

    for name in allowlist:
        if name in already:
            continue  # explicit env wins
        val = parsed.get(name)
        if val:
            target[name] = val
            loaded[name] = fingerprint(val)

    return CredentialLoadReport(loaded=loaded, already_present=already, dotenv_present=True)


def format_report(report: CredentialLoadReport) -> str:
    """A secret-free one-line summary for boot logs."""
    if not report.dotenv_present:
        return "[CredentialBootstrap] no .env — relying on exported env (no-op)"
    if report.error:
        return f"[CredentialBootstrap] {report.error} — relying on exported env"
    loaded = ", ".join(f"{n}={fp}" for n, fp in sorted(report.loaded.items())) or "<none>"
    kept = ", ".join(sorted(report.already_present)) or "<none>"
    return (
        f"[CredentialBootstrap] loaded-from-.env: {loaded} | "
        f"explicit-env-kept: {kept}"
    )


__all__ = [
    "PROVIDER_CREDENTIAL_ALLOWLIST",
    "fingerprint",
    "CredentialLoadReport",
    "parse_dotenv",
    "load_provider_credentials",
    "format_report",
]
