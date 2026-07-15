#!/usr/bin/env python3
"""Slice 25 — prefetch fastembed ONNX weights into the durable local cache.

fastembed defaults its weight cache to ``tempfile.gettempdir()/fastembed_cache``
— an EPHEMERAL dir the OS cleans, so a soak whose harness environment blocks
outbound huggingface traffic hits ``fastembed unavailable (ValueError)`` the
first time the temp cache is cold, and the GIL-holding stdlib TF-IDF fallback
takes over for the whole run.

Run this ONCE (network required only here) so every later in-harness load is a
local file read from the durable cache the code now pins
(``semantic_index._fastembed_cache_dir`` → ``JARVIS_FASTEMBED_CACHE_DIR`` env,
default ``.jarvis/fastembed_cache``):

    python3 scripts/prefetch_fastembed.py

Also salvages an existing tempdir cache (no network) when one is present.
Exit 0 = cache ready + verified with a real encode; exit 1 = not available.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# Repo root on sys.path (cwd-independent, mirrors aegis preflight discipline).
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
os.chdir(_REPO)

from backend.core.ouroboros.governance.semantic_index import (  # noqa: E402
    _fastembed_cache_dir,
)

MODEL = os.environ.get("JARVIS_SEMANTIC_EMBEDDER_MODEL", "BAAI/bge-small-en-v1.5")


def main() -> int:
    dest = _fastembed_cache_dir()
    if dest is None:
        print("FAIL: durable cache dir could not be created", file=sys.stderr)
        return 1

    # 1. Salvage an existing ephemeral cache first (zero network).
    tmp_cache = Path(tempfile.gettempdir()) / "fastembed_cache"
    if tmp_cache.is_dir():
        for entry in tmp_cache.iterdir():
            target = dest / entry.name
            if not target.exists():
                try:
                    shutil.copytree(entry, target)
                    print(f"salvaged from tempdir: {entry.name}")
                except Exception as exc:  # noqa: BLE001
                    print(f"salvage skipped {entry.name}: {exc}", file=sys.stderr)

    # 2. Load through the durable cache (downloads only if still missing),
    #    then PROVE it with a real encode.
    try:
        from fastembed import TextEmbedding
        model = TextEmbedding(model_name=MODEL, cache_dir=str(dest))
        vec = list(model.embed(["prefetch verification probe"]))
        dim = len(vec[0])
        print(f"OK: {MODEL} ready in {dest} (dim={dim})")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
