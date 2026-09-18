"""The corpus must say which model actually ran.

`record_generation`'s main call site passed no `model_id_override`, so the
recorder fell back to `traj.model_id` — the nominal brain-catalog slot, which
still carries legacy GCP ids. Measured: 484 of 2,896 rows (16.7%) produced by
`qwen3-coder-ov:30b` were filed under `qwen-2.5-coder-7b`.

DPO pairs key on `model_id`. A pair built across two models that were in fact
ONE model is not a preference; it is noise presented as signal, and nothing
downstream can detect it.

## Why the repair needs evidence rather than a rule

"Rewrite every 7b row as the 30B" is wrong and provably so: `qwen2.5-coder:7b`
IS installed on this host, so such a row may be perfectly correct. And the
obvious evidence — the `[PrimeProvider] Generated ... model=` log line — is
produced by `reported_model_name`, which carries the very fallback under
investigation. Adjudicating on it would launder the bug into its own evidence;
it initially "proved" that 88 rows came from a real 7B, and `ModelPhysics` —
which reports what the ENDPOINT said about the model it serves — showed the 30B
had been resident for every one of them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.observability import (
    provenance_reconciler as PR,
)


def _session(root: Path, name: str, *physics: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        f"2026-09-18T10:00:00 [Ouroboros.ModelPhysics] INFO [ModelPhysics] "
        f"{m}: native_context=262144 kv_per_token=98304" for m in physics
    )
    (d / "debug.log").write_text(body or "no physics here\n", encoding="utf-8")


def _corpus(root: Path, rows) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / "experience_20260918.jsonl"
    p.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    return p


def _row(sess, model, **kw):
    r = {"event_id": kw.pop("eid", f"e-{sess}-{model}"), "session_id": sess,
         "model_id": model, "tokens_used": 100, "user_input": "p",
         "assistant_output": "a"}
    r.update(kw)
    return r


@pytest.fixture()
def env(tmp_path: Path):
    sessions = tmp_path / "sessions"
    corpus = tmp_path / "events"
    return sessions, corpus


# --------------------------------------------------------------------------
# Adjudication — only PROVEN rows are touched
# --------------------------------------------------------------------------

def test_a_misattributed_row_is_migrated(env):
    sessions, corpus = env
    _session(sessions, "s1", "qwen3-coder-ov:30b")
    _corpus(corpus, [_row("s1", "qwen-2.5-coder-7b")])

    rep = PR.reconcile_corpus(corpus, sessions, dry_run=False)
    assert rep.migrated_rows == 1
    rows = [json.loads(l) for l in
            (corpus / "experience_20260918.jsonl").read_text().splitlines() if l.strip()]
    assert rows[0]["model_id"] == "qwen3-coder-ov:30b"
    assert rows[0]["model_id_reconciled_from"] == "qwen-2.5-coder-7b"
    assert rows[0]["model_id_reconciled_evidence"] == "ModelPhysics"


def test_a_row_whose_session_REALLY_served_a_7b_is_left_alone(env):
    """The repair that would create the same corruption in reverse. A 7B is
    installed on this host; a row naming it may be correct."""
    sessions, corpus = env
    _session(sessions, "s1", "qwen2.5-coder:7b")
    _corpus(corpus, [_row("s1", "qwen2.5-coder:7b")])

    rep = PR.reconcile_corpus(corpus, sessions, dry_run=False)
    assert rep.migrated_rows == 0
    assert rep.verified_rows == 1


def test_a_session_with_no_evidence_is_never_guessed(env):
    sessions, corpus = env
    _session(sessions, "s1")                       # log with no ModelPhysics
    _corpus(corpus, [_row("s1", "qwen-2.5-coder-7b")])

    rep = PR.reconcile_corpus(corpus, sessions, dry_run=False)
    assert rep.migrated_rows == 0
    assert rep.unverifiable_rows == 1
    rows = [json.loads(l) for l in
            (corpus / "experience_20260918.jsonl").read_text().splitlines() if l.strip()]
    assert rows[0]["model_id"] == "qwen-2.5-coder-7b"     # untouched


def test_a_session_that_served_SEVERAL_models_is_ambiguous(env):
    """Two models named means no single row can be attributed from the session
    alone."""
    sessions, corpus = env
    _session(sessions, "s1", "qwen3-coder-ov:30b", "qwen2.5-coder:7b")
    _corpus(corpus, [_row("s1", "qwen-2.5-coder-7b")])

    rep = PR.reconcile_corpus(corpus, sessions, dry_run=False)
    assert rep.migrated_rows == 0
    assert rep.unverifiable_rows == 1


def test_a_row_from_an_unknown_session_is_untouched(env):
    sessions, corpus = env
    sessions.mkdir(parents=True, exist_ok=True)
    _corpus(corpus, [_row("gone", "qwen-2.5-coder-7b")])

    rep = PR.reconcile_corpus(corpus, sessions, dry_run=False)
    assert rep.migrated_rows == 0
    assert rep.unverifiable_rows == 1


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------

def test_dry_run_writes_nothing(env):
    sessions, corpus = env
    _session(sessions, "s1", "qwen3-coder-ov:30b")
    p = _corpus(corpus, [_row("s1", "qwen-2.5-coder-7b")])
    original = p.read_text()

    rep = PR.reconcile_corpus(corpus, sessions, dry_run=True)
    assert rep.migrated_rows == 1          # it SAYS what it would do
    assert p.read_text() == original       # and does none of it
    assert not list(corpus.glob("*.bak-*"))


def test_an_applied_run_backs_up_first(env):
    sessions, corpus = env
    _session(sessions, "s1", "qwen3-coder-ov:30b")
    p = _corpus(corpus, [_row("s1", "qwen-2.5-coder-7b")])
    original = p.read_text()

    rep = PR.reconcile_corpus(corpus, sessions, dry_run=False)
    baks = list(corpus.glob("*.bak-*"))
    assert len(baks) == 1 and baks[0].read_text() == original
    assert rep.backups


def test_it_is_idempotent(env):
    sessions, corpus = env
    _session(sessions, "s1", "qwen3-coder-ov:30b")
    _corpus(corpus, [_row("s1", "qwen-2.5-coder-7b")])

    first = PR.reconcile_corpus(corpus, sessions, dry_run=False)
    second = PR.reconcile_corpus(corpus, sessions, dry_run=False)
    assert first.migrated_rows == 1
    assert second.migrated_rows == 0


def test_every_other_field_survives_byte_for_byte(env):
    sessions, corpus = env
    _session(sessions, "s1", "qwen3-coder-ov:30b")
    row = _row("s1", "qwen-2.5-coder-7b", tokens_used=4242,
               tokens_per_second=151.1, outcome="applied")
    _corpus(corpus, [row])

    PR.reconcile_corpus(corpus, sessions, dry_run=False)
    after = json.loads(
        (corpus / "experience_20260918.jsonl").read_text().splitlines()[0])
    for k, v in row.items():
        if k == "model_id":
            continue
        assert after[k] == v, k


def test_a_malformed_line_is_preserved_verbatim(env):
    sessions, corpus = env
    _session(sessions, "s1", "qwen3-coder-ov:30b")
    corpus.mkdir(parents=True, exist_ok=True)
    p = corpus / "experience_20260918.jsonl"
    p.write_text('{"broken": \n' + json.dumps(_row("s1", "qwen-2.5-coder-7b")) + "\n",
                 encoding="utf-8")

    PR.reconcile_corpus(corpus, sessions, dry_run=False)
    assert '{"broken":' in p.read_text()


def test_a_missing_corpus_never_raises(tmp_path: Path):
    rep = PR.reconcile_corpus(tmp_path / "nope", tmp_path / "also-nope")
    assert rep.migrated_rows == 0


def test_it_is_OFF_by_default(monkeypatch):
    """A boot pass that rewrites the training corpus is not armed by default."""
    monkeypatch.delenv(PR._ENV_ENABLED, raising=False)
    assert PR.reconciler_enabled() is False
    monkeypatch.setenv(PR._ENV_ENABLED, "true")
    assert PR.reconciler_enabled() is True


def test_the_evidence_is_NOT_the_line_that_carries_the_bug(env):
    """`PrimeProvider] Generated ... model=` comes from `reported_model_name`,
    which has the fallback under investigation. A session log containing ONLY
    that line must not be treated as evidence."""
    sessions, corpus = env
    d = sessions / "s1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "debug.log").write_text(
        "[Ouroboros.Providers] INFO [PrimeProvider] Generated 1 candidates in "
        "3.0s, model=qwen-2.5-coder-7b, tokens=1+1\n", encoding="utf-8")
    _corpus(corpus, [_row("s1", "qwen-2.5-coder-7b")])

    rep = PR.reconcile_corpus(corpus, sessions, dry_run=False)
    assert rep.migrated_rows == 0
    assert rep.unverifiable_rows == 1
