"""Nothing leaves this host with a credential in it.

`RepairTrajectoryEmitter` streams a preference pair to Reactor-Core, and the
payload carries whole candidate source in `assistant_output`,
`original_response` and `corrected_response`. The destination is off-host.

So this is the one guard in the tree that fails CLOSED. Everywhere else,
degrading to pass-through protects the FSM; here it would turn a scrubber
bug into a credential disclosure, and bytes that have left cannot be
recalled. Losing a training sample costs one row in a DPO corpus.

Most of these tests are therefore about what must NOT survive the scrubber,
and the rest about the payload being dropped rather than sent when the
scrubber cannot do its job.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.egress_redactor import (
    REDACTED,
    RedactionReport,
    redact_payload,
    redact_text,
)


def _scrub(text: str) -> str:
    return redact_text(text)[0]


# ---------------------------------------------------------------------------
# Named secrets — the name is the signal, whatever the value looks like
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    'API_KEY = "whatever-this-is"',
    'api_key = "whatever-this-is"',
    'password: str = "hunter2"',
    'CLIENT_SECRET = "abc"',
    'private_key = "-----BEGIN"',
    'auth_token = "zzz"',
    'AWS_SECRET_ACCESS_KEY = "abcdef"',
])
def test_named_assignments_are_redacted(line):
    """This pass catches a credential no token pattern would recognise --
    the value can be any string at all."""
    out = _scrub(line)
    assert REDACTED in out
    assert "hunter2" not in out and "whatever-this-is" not in out


def test_env_shape_is_redacted():
    out = _scrub("export API_KEY=sk-live-aaaaaaaaaaaaaaaaaaaaaa\nPORT=8080\n")
    assert "sk-live" not in out
    assert "PORT=8080" in out, "a non-secret env line must survive"


def test_mapping_entries_are_redacted():
    out = _scrub('{"api_key": "zzz", "count": 3}')
    assert "zzz" not in out
    assert "3" in out


def test_ordinary_values_survive():
    """Over-redaction destroys the training data this payload exists to be."""
    src = 'name = "jarvis"\ncount = 3\nurl = "https://example.com/docs"\n'
    assert _scrub(src) == src


# ---------------------------------------------------------------------------
# Token shapes — a secret that was never assigned to an obvious name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret", [
    "sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "sk-ant-aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "xoxb-1111111111-abcdefghij",
    "AKIAIOSFODNN7EXAMPLE",
    "AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
])
def test_known_token_shapes_are_redacted_anywhere(secret):
    out = _scrub(f"# a comment mentioning {secret} in passing\n")
    assert secret not in out
    assert REDACTED in out


def test_pem_block_is_redacted():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = _scrub(f"KEY = '''{pem}'''")
    assert "MIIEowIBAAKCAQEA" not in out


def test_jwt_is_redacted():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop"
    assert jwt not in _scrub(f"token = {jwt}")


def test_url_credentials_are_redacted_but_the_host_survives():
    """The host is useful context; the credentials are not."""
    out = _scrub('DB = "postgres://admin:s3cret@db.internal:5432/app"')
    assert "s3cret" not in out and "admin" not in out
    assert "db.internal" in out


# ---------------------------------------------------------------------------
# AST pass — what a line-oriented regex cannot see
# ---------------------------------------------------------------------------


def test_multiline_secret_literal_is_caught():
    src = 'API_KEY = (\n    "part-one-"\n    "part-two"\n)\n'
    out = _scrub(src)
    assert "part-two" not in out or REDACTED in out


def test_scrubbed_python_still_parses():
    """Redaction preserves structure -- the payload has to remain usable as
    training data, not become a syntax error."""
    import ast
    src = 'API_KEY = "sk-aaaaaaaaaaaaaaaaaaaa"\n\n\ndef f(x):\n    return x + 1\n'
    ast.parse(_scrub(src))


def test_non_python_text_is_still_scrubbed():
    """A candidate may be JSON or YAML; the regex passes must still run."""
    out = _scrub('{"password": "hunter2"}')
    assert "hunter2" not in out


# ---------------------------------------------------------------------------
# Fail CLOSED
# ---------------------------------------------------------------------------


def test_unscrubbable_member_drops_the_whole_payload():
    """Degrading to pass-through here would convert a bug into a
    disclosure. The payload is dropped instead."""
    scrubbed, report = redact_payload({"code": "x = 1", "obj": object()})
    assert scrubbed is None
    assert report.dropped is True
    assert report.reason


def test_deep_nesting_drops_rather_than_recursing_forever():
    node: object = "leaf"
    for _ in range(64):
        node = {"n": node}
    scrubbed, report = redact_payload(node)
    assert scrubbed is None
    assert report.dropped is True


def test_a_clean_payload_passes_through_intact():
    payload = {"user_input": "repair the thing", "metadata": {"n": 3}}
    scrubbed, report = redact_payload(payload)
    assert scrubbed == payload
    assert report.clean is True


def test_payload_secrets_are_scrubbed_recursively():
    payload = {
        "assistant_output": 'API_KEY = "sk-aaaaaaaaaaaaaaaaaaaa"',
        "metadata": {"nested": ['password = "hunter2"']},
    }
    scrubbed, report = redact_payload(payload)
    assert "sk-aaaa" not in str(scrubbed)
    assert "hunter2" not in str(scrubbed)
    assert report.redactions >= 2
    assert report.dropped is False


def test_report_names_what_it_removed():
    """A drop that is not in the record is indistinguishable from a payload
    that never had a secret."""
    _out, report = redact_text('API_KEY = "sk-aaaaaaaaaaaaaaaaaaaa"')
    assert report.redactions >= 1
    assert report.kinds
    assert "redacted" in report.render()


def test_empty_and_non_string_inputs_are_safe():
    assert redact_text("")[0] == ""
    assert redact_text(None)[0] == ""  # type: ignore[arg-type]
    assert RedactionReport().clean is True


def test_numbers_and_bools_survive_the_walk():
    payload = {"a": 1, "b": 2.5, "c": True, "d": None}
    scrubbed, report = redact_payload(payload)
    assert scrubbed == payload
    assert report.dropped is False
