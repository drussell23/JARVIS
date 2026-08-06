"""
Cleartext credential eradication -- the single canonical seam.

WHY THIS MODULE EXISTS
----------------------
Voice unlock was built on a premise that cannot work on this platform: retrieve
the macOS login password in cleartext, then synthesise keystrokes into the lock
screen. Measured evidence (47/47 sessions in ``~/.jarvis/logs/unlock_metrics``,
every one reporting 13/13 characters typed with ``screen_locked=1`` before and
after) shows the keystrokes are posted into a ``SecureEventInput`` void. No
process in the Aqua session can reach the ``SecurityAgent`` password field, so
no amount of repair to the typing path can succeed.

That makes the stored credential pure liability: it bought nothing and it sat in
the login keychain without an ACL, readable in cleartext by any process running
as the user. The replacement is an Authorization Plugin that participates in the
``system.login.screensaver`` right directly, so no password is ever retrieved,
held, typed, or stored.

This module is the ONE place that policy is written down. Every former retrieval
site imports from here and raises, so the vulnerability cannot be reintroduced by
editing a single call site -- a would-be resurrector has to delete this module,
which the regression suite forbids.

ON "ZEROING" MEMORY -- AN HONEST NOTE
-------------------------------------
CPython ``str`` is immutable and interned; there is no supported way to overwrite
one in place. Any claim to "zero the password out of memory" for a ``str`` is
theatre. What this module actually does is drop every *reference* so the object
becomes unreachable, and zero the buffers it genuinely can (``bytearray`` and
other writable buffers). The real guarantee is structural rather than hygienic:
after the sweep there is no code path that puts a login credential in memory at
all.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, MutableMapping, NoReturn, Optional, TypeVar

#: Value type of a mapping being sanitised. ``MutableMapping`` is invariant in
#: its value type, so a plain ``MutableMapping[str, object]`` parameter would
#: reject ``os.environ`` (a ``MutableMapping[str, str]``). The TypeVar keeps the
#: helper usable for both the environment and arbitrary config singletons.
_V = TypeVar("_V")

logger = logging.getLogger(__name__)

__all__ = [
    "SecurityError",
    "CleartextCredentialEradicated",
    "EradicatedCredential",
    "ERADICATION_NOTICE",
    "eradicated_credentials",
    "eradicated_env_keys",
    "eradicated_path",
    "sanitize_process_environment",
    "sanitize_mapping",
    "zero_buffer",
]


# =============================================================================
# EXCEPTIONS
# =============================================================================


class SecurityError(Exception):
    """Base class for zero-trust policy violations raised by this package."""


class CleartextCredentialEradicated(SecurityError):
    """
    Raised when code attempts a cleartext credential path that has been removed.

    This is deliberately a hard failure rather than a ``None`` return. A soft
    return would let callers fall through to the next fallback in a cascade and
    rediscover the same dead end; the raise stops the cascade at the seam and
    names the replacement.
    """

    def __init__(self, subject: str, replacement: Optional[str] = None) -> None:
        message = f"{ERADICATION_NOTICE} (blocked path: {subject})"
        if replacement:
            message = f"{message} Use {replacement} instead."
        super().__init__(message)
        self.subject = subject
        self.replacement = replacement


ERADICATION_NOTICE = (
    "Cleartext password retrieval permanently eradicated for zero-trust compliance."
)

#: Named replacement for every eradicated path, so the error is actionable
#: rather than merely prohibitive.
DEFAULT_REPLACEMENT = (
    "the JARVIS Authorization Plugin grant flow "
    "(backend/voice_unlock/authplugin, backend/voice_unlock/grant_bridge.py)"
)


# =============================================================================
# CREDENTIAL REGISTRY
# =============================================================================


@dataclass(frozen=True)
class EradicatedCredential:
    """
    A credential whose cleartext retrieval path has been removed.

    ``service``/``account`` are the keychain coordinates. They live here rather
    than being spelled out at each call site so the guard, the deletion tooling,
    and the regression tests all read the same source of truth -- there is no
    second copy to drift.
    """

    service: str
    account: str
    env_keys: frozenset[str] = field(default_factory=frozenset)
    reason: str = ""

    @property
    def coordinates(self) -> tuple[str, str]:
        return (self.service, self.account)


#: The macOS login password formerly used by the keystroke-synthesis unlock path.
_LOGIN_UNLOCK_CREDENTIAL = EradicatedCredential(
    service="com.jarvis.voiceunlock",
    account="unlock_token",
    env_keys=frozenset({"JARVIS_UNLOCK_PASS", "JARVIS_UNLOCK_PASSWORD"}),
    reason=(
        "macOS SecureEventInput makes lock-screen keystroke synthesis impossible; "
        "the Authorization Plugin grants the right without a password"
    ),
)

_BUILTIN_CREDENTIALS: tuple[EradicatedCredential, ...] = (_LOGIN_UNLOCK_CREDENTIAL,)

#: Extension point. Comma-separated ``service:account`` pairs; extends the
#: builtin registry rather than replacing it, so an operator can widen the sweep
#: without being able to silently narrow it.
ENV_EXTRA_CREDENTIALS = "JARVIS_ERADICATED_CREDENTIALS"

#: Extension point for additional environment keys to sweep. Additive, same
#: rationale as above.
ENV_EXTRA_ENV_KEYS = "JARVIS_ERADICATED_ENV_KEYS"

#: Environment keys matching this shape are swept even if not enumerated. Scoped
#: deliberately to unlock/login credentials -- a broad "anything secret-looking"
#: pattern would strip provider API keys and break the running system.
_ENV_KEY_PATTERN = re.compile(
    r"^JARVIS_(?:[A-Z0-9_]*_)?(?:UNLOCK|LOGIN)_(?:PASS|PASSWORD|SECRET|TOKEN)$"
)


def _parse_extra_credentials(raw: Optional[str]) -> tuple[EradicatedCredential, ...]:
    """Parse ``service:account[,service:account...]`` into descriptors."""
    if not raw:
        return ()
    parsed: list[EradicatedCredential] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        service, sep, account = chunk.partition(":")
        if not sep or not service.strip() or not account.strip():
            logger.warning(
                "Ignoring malformed %s entry %r (expected 'service:account')",
                ENV_EXTRA_CREDENTIALS,
                chunk,
            )
            continue
        parsed.append(
            EradicatedCredential(
                service=service.strip(),
                account=account.strip(),
                reason=f"declared via {ENV_EXTRA_CREDENTIALS}",
            )
        )
    return tuple(parsed)


def eradicated_credentials(
    env: Optional[Mapping[str, str]] = None,
) -> tuple[EradicatedCredential, ...]:
    """Return the builtin registry plus any operator-declared additions."""
    source = os.environ if env is None else env
    return _BUILTIN_CREDENTIALS + _parse_extra_credentials(source.get(ENV_EXTRA_CREDENTIALS))


def eradicated_env_keys(env: Optional[Mapping[str, str]] = None) -> frozenset[str]:
    """
    Every environment key known to have carried a cleartext login credential.

    Combines the registry's declared keys, operator-declared additions, and any
    key in ``env`` matching the narrowly-scoped credential shape.
    """
    source = os.environ if env is None else env

    keys: set[str] = set()
    for credential in eradicated_credentials(source):
        keys.update(credential.env_keys)

    extra = source.get(ENV_EXTRA_ENV_KEYS, "")
    keys.update(part.strip() for part in extra.split(",") if part.strip())

    keys.update(key for key in source if _ENV_KEY_PATTERN.match(key))

    return frozenset(keys)


# =============================================================================
# THE GUARD
# =============================================================================


def eradicated_path(subject: str, replacement: Optional[str] = DEFAULT_REPLACEMENT) -> NoReturn:
    """
    Refuse an eradicated cleartext-credential path.

    Args:
        subject: Fully-qualified name of the blocked path, e.g.
            ``"MacOSKeychainUnlock.get_password_from_keychain"``. Appears in the
            message so the failure names itself without a traceback read.
        replacement: What the caller should use instead.

    Raises:
        CleartextCredentialEradicated: Always.
    """
    logger.error(
        "Blocked eradicated cleartext-credential path: %s (replacement: %s)",
        subject,
        replacement or "none",
    )
    raise CleartextCredentialEradicated(subject, replacement)


# =============================================================================
# MEMORY / ENVIRONMENT SANITISATION
# =============================================================================


def zero_buffer(buffer: object) -> bool:
    """
    Overwrite a writable buffer in place.

    Returns True if the buffer was actually zeroed. Immutable objects (``str``,
    ``bytes``) return False rather than pretending -- see the module docstring.
    """
    try:
        view = memoryview(buffer)  # type: ignore[arg-type]
    except TypeError:
        return False
    if view.readonly:
        return False
    try:
        view[:] = b"\x00" * view.nbytes
        return True
    except (TypeError, ValueError):
        return False
    finally:
        view.release()


def sanitize_mapping(mapping: MutableMapping[str, _V], keys: Iterable[str]) -> frozenset[str]:
    """
    Remove the given keys from a mutable mapping, zeroing writable values.

    Used for both ``os.environ`` and configuration singletons that cached the
    legacy value. Plain dict operations, per the DRY mandate -- no bespoke
    framework.
    """
    removed: set[str] = set()
    for key in list(keys):
        if key not in mapping:
            continue
        try:
            value = mapping[key]
        except Exception:  # pragma: no cover - exotic mapping
            value = None
        zero_buffer(value)
        try:
            del mapping[key]
        except Exception as exc:  # pragma: no cover - exotic mapping
            logger.warning("Could not delete %s during sanitisation: %s", key, exc)
            continue
        removed.add(key)
    return frozenset(removed)


def sanitize_process_environment(
    env: Optional[MutableMapping[str, str]] = None,
) -> frozenset[str]:
    """
    Pop every eradicated credential key from the process environment.

    Idempotent and safe to call at any point in boot. Returns the keys actually
    removed so callers can log a real number rather than an assumption.
    """
    target: MutableMapping[str, str] = os.environ if env is None else env
    removed = sanitize_mapping(target, eradicated_env_keys(target))
    if removed:
        logger.warning(
            "Credential sanitisation removed %d legacy env key(s) from the process "
            "environment: %s",
            len(removed),
            ", ".join(sorted(removed)),
        )
    else:
        logger.debug("Credential sanitisation: no legacy env keys present")
    return removed
