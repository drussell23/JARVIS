from __future__ import annotations
from backend.core.ouroboros.governance import epistemic_prefetch as ep
from backend.core.ouroboros.governance.epistemic_prefetch import PrefetchEntry
from backend.core.ouroboros.governance.epistemic_quarantine import (
    QuarantineLedger, sha256_of_file,
)


def test_revalidate_keeps_fresh_drops_stale(tmp_path):
    f = tmp_path / "fresh.py"
    f.write_text("x = 1\n", encoding="utf-8")
    fresh_h = sha256_of_file(str(f))
    s = tmp_path / "stale.py"
    s.write_text("y = 2\n", encoding="utf-8")  # on disk now
    manifest = (
        PrefetchEntry("fresh.py", fresh_h, 0.9, "CALL_GRAPH", "x = 1"),
        PrefetchEntry("stale.py", "deadbeef_wrong_hash", 0.5, "COMPREHENSION", "old"),
        PrefetchEntry("gone.py", "anyhash", 0.4, "COMPREHENSION", "missing"),  # not on disk
    )
    out = ep.revalidate_manifest(manifest, str(tmp_path), ledger=None)
    rels = {e.rel_path for e in out}
    assert rels == {"fresh.py"}   # stale + missing dropped, fresh kept


def test_revalidate_quarantines_stale(tmp_path):
    s = tmp_path / "stale.py"
    s.write_text("y = 2\n", encoding="utf-8")
    led = QuarantineLedger(path=str(tmp_path / "q.jsonl"), session_id="S1")
    manifest = (PrefetchEntry("stale.py", "wrong_hash", 0.5, "COMPREHENSION", "old"),)
    ep.revalidate_manifest(manifest, str(tmp_path), ledger=led)
    assert led.is_quarantined("stale.py") is True


def test_revalidate_empty_is_empty():
    assert ep.revalidate_manifest((), "/tmp", ledger=None) == ()
