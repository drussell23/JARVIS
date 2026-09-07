"""Root-cause regression for the multi-file cadence wall: a mid-size model
splits ONE multi-file change across sibling single-file candidates (c1=fileA,
c2=fileB). The Iron Gate coverage check is applied to EVERY candidate and
rejects the whole generation if any is partial, so the split never lands even
though every file was generated. normalize_candidate_set reassembles the set so
every surviving candidate covers all targets."""
from __future__ import annotations

from backend.core.ouroboros.governance import multi_file_coverage_gate as G


A = "tests/governance/comms/karen_synth/test_persona.py"
B = "tests/governance/comms/karen_synth/test_speech_provider.py"


def _sf(path, content="x = 1\n", cid="c"):
    return {"candidate_id": cid, "file_path": path, "full_content": content,
            "rationale": f"edit {path}", "source_hash": "s", "source_path": path}


def _mf(paths, cid="cm"):
    return {"candidate_id": cid,
            "files": [{"file_path": p, "full_content": f"# {p}\nx=1\n"} for p in paths],
            "rationale": "multi"}


def test_single_target_is_untouched():
    cands = (_sf(A),)
    assert G.normalize_candidate_set(cands, [A], None) == cands


def test_two_per_file_candidates_compose_into_one_multifile(monkeypatch):
    monkeypatch.setenv("JARVIS_MULTIFILE_COMPOSE_ENABLED", "true")
    cands = (_sf(A, "def test_persona_x():\n    assert True\n", "c1"),
             _sf(B, "def test_speech_x():\n    assert True\n", "c2"))
    out = G.normalize_candidate_set(cands, [A, B], None)
    assert len(out) == 1, out
    composed = out[0]
    files = composed.get("files")
    assert isinstance(files, list) and len(files) == 2
    covered = {f["file_path"] for f in files}
    assert covered == {A, B}
    # the composed candidate must satisfy the real coverage gate
    assert G.check_candidate(composed, [A, B], None) is None
    # each file carries the model's actual content
    persona = next(f for f in files if f["file_path"] == A)
    assert "test_persona_x" in persona["full_content"]


def test_complete_candidate_plus_partial_sibling_keeps_only_complete():
    cands = (_mf([A, B], "good"), _sf(A, cid="junk"))
    out = G.normalize_candidate_set(cands, [A, B], None)
    assert len(out) == 1
    assert out[0]["candidate_id"] == "good"
    assert G.check_candidate(out[0], [A, B], None) is None


def test_genuine_partial_coverage_is_left_for_the_gate():
    # only one of two targets has a candidate -> cannot compose -> unchanged
    cands = (_sf(A, cid="c1"),)
    out = G.normalize_candidate_set(cands, [A, B], None)
    assert tuple(out) == cands


def test_all_complete_is_byte_identical():
    cands = (_mf([A, B]),)
    out = G.normalize_candidate_set(cands, [A, B], None)
    assert tuple(out) == cands


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("JARVIS_MULTIFILE_COMPOSE_ENABLED", "false")
    cands = (_sf(A, cid="c1"), _sf(B, cid="c2"))
    out = G.normalize_candidate_set(cands, [A, B], None)
    assert tuple(out) == cands


def test_empty_is_noop():
    assert G.normalize_candidate_set((), [A, B], None) == ()


def test_failsoft_on_garbage_returns_input():
    cands = ("not-a-dict",)  # type: ignore[list-item]
    out = G.normalize_candidate_set(cands, [A, B], None)
    assert tuple(out) == tuple(cands)
