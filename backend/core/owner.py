"""
owner - who JARVIS is talking to, answered once.

Six places named the operator, each its own way: ``JARVIS_OWNER_NAME`` with
"Sir", ``JARVIS_USER_NAME`` with "Sir", ``$USER`` (absent on Windows) with
"there", the login name ("Jarvis" -- the account, not a person), and a literal
"Derek". One boot greeted "there", "Jarvis" and "Derek".

The answer, best evidence first:

1. declared -- ``JARVIS_OWNER_NAME`` (``JARVIS_USER_NAME`` is read as its alias);
2. enrolled -- the primary user of the voice profiles, once
   OwnerIdentityService has loaded them;
3. the OS account's full name (:func:`host_platform.account_full_name`);
4. the declared form of address, ``JARVIS_OWNER_ADDRESS`` (default "Sir",
   how JARVIS addresses someone he cannot name).

Greetings use the first name.
"""
from __future__ import annotations

import os
import sys

_NAME_VARS = ("JARVIS_OWNER_NAME", "JARVIS_USER_NAME")


def declared_owner_name() -> str:
    """The name the operator declared, or ""."""
    for var in _NAME_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return ""


def form_of_address() -> str:
    """How JARVIS addresses an operator he cannot name."""
    return os.environ.get("JARVIS_OWNER_ADDRESS", "").strip() or "Sir"


def _enrolled_owner_name() -> str:
    """The primary voice-profile user, if OwnerIdentityService has loaded one.
    Read without importing (or initialising) the service."""
    for module_name in ("backend.agi_os.owner_identity_service", "agi_os.owner_identity_service"):
        module = sys.modules.get(module_name)
        service = getattr(module, "_identity_service", None) if module else None
        owner = getattr(service, "_current_owner", None)
        source = getattr(getattr(owner, "identity_source", None), "value", "")
        if owner and source in ("voice_biometric", "cached_profile", "database_profile"):
            return owner.name
    return ""


def owner_full_name() -> str:
    """The operator's name by the best evidence available, or the form of
    address when there is none."""
    from backend.core.host_platform import account_full_name

    return (declared_owner_name() or _enrolled_owner_name()
            or account_full_name() or form_of_address())


def owner_name() -> str:
    """What JARVIS calls the operator: a first name, or the form of address."""
    full = owner_full_name()
    return full.split()[0] if full.split() else form_of_address()


__all__ = ["declared_owner_name", "form_of_address", "owner_full_name", "owner_name"]
