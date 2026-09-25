"""Docstrings and comments are published too.

The scanner used to skip every docstring by construction. A model asked for an
"example request" will happily paste a real key into one, and a text scanner on
the receiving end reads it like any other byte. Prose is now scanned for
complete credential shapes, and for random runs no language produces --
calibrated on this repository's 2,762 prose tokens (see ``_prose_suspect``).

Every key below is generated at runtime; none exists in this file.
"""
from __future__ import annotations

import hashlib
import importlib.util
import random
import string
import sys
import uuid
from pathlib import Path

import pytest

from tests.support import fake_credentials as fake

_SCANNER = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "scan_secrets.py"
_spec = importlib.util.spec_from_file_location("scan_secrets", _SCANNER)
scan_secrets = importlib.util.module_from_spec(_spec)
sys.modules["scan_secrets"] = scan_secrets
_spec.loader.exec_module(scan_secrets)


def _findings(source: str) -> list:
    return [(f.line, f.name, f.kind) for f in scan_secrets.scan_source(source, "t.py")]


def _docstring(*body_lines: str) -> str:
    return 'def endpoint():\n    """Call the API.\n\n' + "".join(
        f"    {line}\n" for line in body_lines
    ) + '    """\n'


def _random_key(rng: random.Random, alphabet: str, length: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# Known shapes -- decisive in prose too
# ---------------------------------------------------------------------------


def test_a_known_key_in_a_docstring_is_caught_on_its_own_line():
    src = _docstring("Example:", f"    headers = {{'x-api-key': '{fake.OPENAI_KEY}'}}")
    assert _findings(src) == [(5, "<docstring>", "OpenAI Key")]


def test_a_known_key_in_a_comment_is_caught():
    src = f"X = 1  # e.g. {fake.AWS_ACCESS_KEY}\n"
    assert _findings(src) == [(1, "<comment>", "AWS Access Key")]


def test_every_shape_in_one_docstring_is_reported():
    src = _docstring(f"aws {fake.AWS_ACCESS_KEY}", f"slack {fake.SLACK_TOKEN}")
    assert sorted(k for _, _, k in _findings(src)) == ["AWS Access Key", "Slack Token"]


def test_a_documented_pem_marker_is_not_a_key():
    """The scanner's own documentation names the marker it looks for."""
    marker = fake.PEM_PRIVATE_KEY.splitlines()[0]
    assert _findings(_docstring(f"PRIVATE_KEY -- {marker} block")) == []


def test_a_pem_block_with_a_body_in_a_docstring_is_a_key():
    rng = random.Random(7)
    body = _random_key(rng, string.ascii_letters + string.digits + "+/", 64)
    head, _body, tail = fake.PEM_PRIVATE_KEY.splitlines()
    kinds = [k for _, _, k in _findings(_docstring(head, body, tail))]
    assert kinds == ["PEM Private Key"]


# ---------------------------------------------------------------------------
# Unprefixed random keys -- the adaptive randomness gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alphabet,length,floor", [
    (string.ascii_letters + string.digits, 32, 0.88),
    (string.ascii_letters + string.digits, 40, 0.90),
    (string.ascii_letters + string.digits + "+/", 40, 0.90),
    (string.ascii_letters + string.digits + "-_", 40, 0.88),
])
def test_random_keys_in_a_docstring_are_caught_at_the_calibrated_rate(alphabet, length, floor):
    """A property, not a hand-picked example: the gate trades a small miss
    rate for zero false positives on real prose, and that rate is pinned."""
    rng = random.Random(length * 1000 + len(alphabet))
    keys = [_random_key(rng, alphabet, length) for _ in range(400)]
    keys = [k for k in keys if any(c.isdigit() for c in k)]
    hit = sum(bool(_findings(_docstring(f"token: {k}"))) for k in keys) / len(keys)
    assert hit >= floor, f"detection {hit:.3f} fell below {floor}"


@pytest.mark.parametrize("token", [
    # Every family the calibration found in real prose, one each.
    "M10AdaptiveThreshold", "JARVIS_GCP_ENABLED=1", "kCGWindowImageDefault=0",
    "docs/benchmarks/DW_BENCHMARKS_2026-04-16", "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8",
    "google/gemma-4-26B-A4B-it", "5-397B-A17B-FP8-dottxt", "min_params_b=14B/30B",
    "op-019d9368-654b-7612-a031-6507ffde327c-cau", "bt-2026-05-27-220220",
    "2xx/3xx/4xx/5xx/other", "slice188/189/190/194/227",
])
def test_language_shaped_tokens_are_not_keys(token):
    assert _findings(_docstring(f"see {token} for details")) == []


def test_hex_digests_and_uuids_are_not_judged_by_entropy():
    """Documented limit: in prose a hex run is overwhelmingly a digest or an
    id, indistinguishable by content from a hex key. Shapes still apply."""
    digest = hashlib.sha256(b"x").hexdigest()
    assert _findings(_docstring(f"sha256 {digest}", f"id {uuid.UUID(int=7)}")) == []


# ---------------------------------------------------------------------------
# The pragma is per line
# ---------------------------------------------------------------------------


def test_the_prose_pragma_covers_its_own_line_only():
    src = _docstring(
        f"reviewed {fake.AWS_ACCESS_KEY}  # pragma: allowlist secret",
        f"not reviewed {fake.SLACK_TOKEN}",
    )
    assert [k for _, _, k in _findings(src)] == ["Slack Token"]


def test_prose_thresholds_are_tunable_without_code(monkeypatch):
    rng = random.Random(3)
    key = _random_key(rng, string.ascii_letters + string.digits, 40)
    src = _docstring(f"token: {key}")
    monkeypatch.setenv("SECRET_SCAN_PROSE_RANDOMNESS", "1.01")   # impossible bar
    assert _findings(src) == []
