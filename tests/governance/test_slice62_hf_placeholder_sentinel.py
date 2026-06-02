"""Slice 62 — Hugging Face token placeholder sentinel.

Recurring misconfig (2026-06-02, three times): the operator's HF auth used an
un-substituted placeholder (`hf_YOUR_REAL_TOKEN_HERE`, `your_actual_token_here`,
`<paste_your_huggingface_token_here>`), which 401s DEEP inside huggingface_hub —
a cryptic traceback that doesn't say "your token is fake."

Fix: a pre-flight sentinel in the HF load path. A placeholder token is a CONFIG
error (not transient), so it aborts loudly with an actionable message BEFORE the
network call — while genuinely transient errors stay fail-open (the loader's
existing contract). An unset/empty token is NOT a placeholder (a disk-cached
`hf auth login` token is valid with HF_TOKEN unset).
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import dataset_loader as dl


def test_predicate_detects_placeholders():
    for p in [
        "hf_YOUR_REAL_TOKEN_HERE",
        "your_actual_token_here",
        "<paste_your_huggingface_token_here>",
        "paste_your_token",
        "YOUR_HUGGINGFACE_TOKEN",
        "your_real_token",
        "token_here",
    ]:
        assert dl.hf_token_appears_placeholder(p) is True, p


def test_predicate_accepts_real_and_empty():
    # A realistic token must pass; empty/unset must NOT trip (disk-cached
    # `hf auth login` token is valid with HF_TOKEN env unset).
    assert dl.hf_token_appears_placeholder("hf_AbC123realLooKing789xyz") is False
    assert dl.hf_token_appears_placeholder("") is False
    assert dl.hf_token_appears_placeholder("   ") is False
    assert dl.hf_token_appears_placeholder(None) is False  # defensive


def test_iter_hf_records_raises_on_placeholder(monkeypatch):
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_HF_DATASET", "ScaleAI/SWE-bench_Pro")
    monkeypatch.setenv("HF_TOKEN", "hf_YOUR_REAL_TOKEN_HERE")
    with pytest.raises(dl.HFTokenPlaceholderError):
        list(dl._iter_hf_records())


def test_iter_hf_records_inert_when_dataset_unset(monkeypatch):
    # No HF dataset configured -> the HF path is inert, placeholder token in
    # env is irrelevant (never reaches the sentinel). Byte-identical to legacy.
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_HF_DATASET", raising=False)
    monkeypatch.setenv("HF_TOKEN", "hf_YOUR_REAL_TOKEN_HERE")
    assert list(dl._iter_hf_records()) == []


def test_iter_all_dataset_records_propagates_placeholder(monkeypatch):
    # The full-scan path (geometric sampler) must surface the config error,
    # not silently fail-open to an empty distribution.
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_HF_DATASET", "ScaleAI/SWE-bench_Pro")
    monkeypatch.setenv("HF_TOKEN", "paste_your_token")
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", raising=False)
    with pytest.raises(dl.HFTokenPlaceholderError):
        list(dl.iter_all_dataset_records())


def test_load_from_huggingface_propagates_placeholder(monkeypatch):
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_HF_DATASET", "ScaleAI/SWE-bench_Pro")
    monkeypatch.setenv("HF_TOKEN", "<paste_your_huggingface_token_here>")
    with pytest.raises(dl.HFTokenPlaceholderError):
        dl._load_from_huggingface("any__instance-1")
