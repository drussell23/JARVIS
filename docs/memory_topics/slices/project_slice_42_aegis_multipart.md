---
title: Project Slice 42 Aegis Multipart
modules: [backend/core/ouroboros/aegis/request_body.py, backend/core/ouroboros/aegis/passthrough.py, backend/core/ouroboros/aegis/forwarding.py, tests/aegis/test_request_body.py, tests/aegis/test_passthrough.py]
status: historical
source: project_slice_42_aegis_multipart.md
---

Slice 42 — Aegis Multi-Segment Payload Assembly & DoS Guard. **MERGED main 2026-05-28** PR #63863 (merge `ebc13b2a21`). Opus security-reviewed READY TO MERGE, no issues. Builds on [[project_slice_41_batch_aware_fleet]].

**The scope-discipline win (operator-flagged as the key lesson):** the runbook ORIGINALLY targeted `doubleword_provider._upload_file` (force Content-Length / strip chunked / hand-append boundaries). A live A/B probe FALSIFIED that: direct (non-Aegis) `/v1/files` uploads return **HTTP 201 at 300B / 4KB / 18KB / 64KB**, BytesIO vs raw bytes identical (aiohttp sets Content-Length for both). `_upload_file` was perfectly correct. **The bug was two layers up, in the Aegis zero-trust passthrough daemon** (the soak routes through `127.0.0.1:8099`).

**ROOT CAUSE:** both Aegis proxy body-read sites used `body_bytes = await request.content.read(cap)`. `aiohttp.StreamReader.read(n)` returns **at most the first buffered TCP segment** ("≤ n, but at least one byte") — so a multi-segment body (18KB multipart batch upload) was **silently TRUNCATED**; Aegis forwarded a broken multipart upstream → DW HTTP 400 "Multipart parsing failed". Single-segment small bodies (the health probe's ~300B "ping", gatekick) passed → `batch_storage: healthy` MASKED it. The cap (4MB) was NOT the issue — the read-semantics were.

**FIX:** new `backend/core/ouroboros/aegis/request_body.py` `read_body_capped(request, cap)` — chunk-accumulation loop (`while True: chunk = await request.content.read(64KB); if not chunk: break; total += len; if total > cap: raise BodyTooLarge`) reads the FULL body and **preserves + actually enforces** the DoS cap (old `read(cap)` silently truncated instead of 413-ing; new raises BodyTooLarge → HTTP 413, bounded memory ≤ cap+1chunk — verified a 1GB stream rejects after one 64KB chunk, can't OOM). Wired at `passthrough.py:232` (multipart `/v1/files` — the culprit) + `forwarding.py:399` (JSON path — same latent bug; a truncated JSON had failed `json.loads` → body_parse_failed). New `REQUEST_TOO_LARGE` outcome on PassthroughOutcome (5→6) + ForwardOutcome (6→7) — no taxonomy-pin tests existed.

**Tests:** 7 socket-free unit (`test_request_body.py` — fragmented multi-chunk reader proves full read + byte-identity; 413 over-cap; exactly-at-cap) + extended `test_passthrough.py` integration byte-identity matrix to 18KB+64KB with full `body_sha256` (old code truncates these). 242 aegis tests pass. NOTE: aegis tests BIND LOCALHOST SOCKETS → fail under sandbox with "Operation not permitted"; run with sandbox disabled.

**Security review (Opus):** DoS-cap bounded-memory verified; behavior isolation confirmed (ONLY request-body read changed — auth/credential-injection/header-stripping/response-streaming untouched); BodyTooLarge cannot escape uncaught.

**v37 capability run (IN FLIGHT at merge):** pure-DW + surface-health on + ledger primed + $1/600s. With Slice 41 (fleet stays open + force-batch) + Slice 42 (Aegis forwards the full 18KB multipart), the chain should now be: fleet open → dispatch → batch routing → upload clears Aegis → batch generates → **first APPLY?**. Per [[feedback_no_preresult_euphoria]]: the v36 win was fleet-open+dispatch+batch-routing (3 firsts) but 0 APPLY (blocked at the 18KB upload); v37 tests whether removing that block reaches APPLY — record the artifact, and the NEXT blocker if one surfaces (VERIFY/GATE each add steps). [TO UPDATE with v37 outcome.]
