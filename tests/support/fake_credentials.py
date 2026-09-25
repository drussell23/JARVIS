"""Inert, format-valid fake credentials for tests — never written whole on disk.

Tests of secret scanners and scrubbers need values that LOOK like real
credentials. Written as literals, those values are indistinguishable from a
leak to every text-level scanner that reads the repository: GitHub Push
Protection rejected ``tests/security/test_adversarial_secrets.py`` for its
Slack and Stripe canaries, and GitGuardian opened an incident on the PEM block
in ``tests/governance/test_egress_redactor.py`` the moment it reached ``main``.

Every value here is therefore ASSEMBLED FROM FRAGMENTS at import time. No
complete credential pattern exists in any file, while the code under test still
receives the fully-formed string and must still recognise it. Allowlisting the
values in a scanner's dashboard was the alternative and is rejected for the
same reason the adversarial canary rejected it: it registers a permanent
"allowed secret" for something that is not a secret at all.

Nothing here authenticates anything: key ids are AWS's documented example,
bodies are repeated filler or published sample tokens.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


def assemble(*fragments: str) -> str:
    """Join *fragments* into one value. The split is the whole point: keep
    every credential prefix and its body in different literals."""
    return "".join(fragments)


OPENAI_KEY = assemble("sk", "-", "a" * 32)
ANTHROPIC_KEY = assemble("sk", "-ant-", "a" * 28)
GITHUB_TOKEN = assemble("gh", "p_", "A" * 30)
SLACK_TOKEN = assemble("xo", "xb-", "1" * 10, "-abcdefghij")
AWS_ACCESS_KEY = assemble("AK", "IA", "IOSFODNN7", "EXAMPLE")
GOOGLE_API_KEY = assemble("AI", "za", "Sy", "A" * 35)
JWT = assemble(
    "ey", "JhbGciOiJIUzI1NiJ9", ".",
    "ey", "JzdWIiOiIxIn0", ".", "abcdefghijklmnop",
)
PEM_PRIVATE_KEY = assemble(
    "-----BEGIN ", "RSA PRIVATE", " KEY-----\n",
    "MIIEowIBAAKCAQEA", "1234567890\n",
    "-----END ", "RSA PRIVATE", " KEY-----",
)
#: The body line of :data:`PEM_PRIVATE_KEY` — what a scrubber must remove.
PEM_BODY = PEM_PRIVATE_KEY.splitlines()[1]


def url_with_credentials(
    *, scheme: str = "postgres", user: str = "admin", password: str = "s3cret",
    host: str = "db.internal:5432", path: str = "app",
) -> str:
    """A connection URL with an embedded ``user:password`` pair."""
    return assemble(scheme, "://", user, ":", password, "@", host, "/", path)


#: Token SHAPES that must be recognised wherever they appear, keyed by kind.
TOKEN_SHAPES: Mapping[str, str] = MappingProxyType({
    "openai": OPENAI_KEY,
    "anthropic": ANTHROPIC_KEY,
    "github": GITHUB_TOKEN,
    "slack": SLACK_TOKEN,
    "aws": AWS_ACCESS_KEY,
    "google": GOOGLE_API_KEY,
})

__all__ = [
    "ANTHROPIC_KEY", "AWS_ACCESS_KEY", "GITHUB_TOKEN", "GOOGLE_API_KEY", "JWT",
    "OPENAI_KEY", "PEM_BODY", "PEM_PRIVATE_KEY", "SLACK_TOKEN", "TOKEN_SHAPES",
    "assemble", "url_with_credentials",
]
