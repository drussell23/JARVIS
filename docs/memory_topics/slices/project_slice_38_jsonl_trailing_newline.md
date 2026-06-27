---
title: Project Slice 38 Jsonl Trailing Newline
modules: [tests/governance/test_slice38_jsonl_trailing_newline.py]
status: historical
source: project_slice_38_jsonl_trailing_newline.md
---

Slice 38 (Canonical JSONL Composer + Trailing Newline) MERGED
2026-05-28 — main @ **2594b2c38e** (PR #63660). Closes the
v25→v33 capability blocker root cause.

**SYMPTOM flagged externally** by DW Support (peter@doubleword.ai,
2026-05-28 ~10:23 AM) — NOT a cause confirmation:

> "We noticed a number of invalid multi part files to our
> /v1/files endpoint from your user. If you think this is a
> mistake please send us the files you are struggling to upload
> and we can verify."

**Provenance precision (do not overclaim):** Peter reported the
SYMPTOM and OFFERED to verify if sent samples; he did NOT confirm
the trailing-newline cause. WE independently diagnosed it and
empirically verified the fix (gatekick HTTP 200). The reading that
"multi part" refers to the multipart file-field CONTENT (not the
multipart envelope) is OUR interpretation — corroborated empirically
because the well-formed FormData envelope (file field + filename +
content_type=application/jsonl + purpose=batch) got HTTP 200 once the
content gained its trailing \n, but it was never stated by DW. Both
`submit_batch` (line 884) and `prompt_only` (line 3472) built
the JSONL upload payload as:

```python
jsonl_line = json.dumps({...})   # ← no trailing \n
```

Structurally valid JSON, **structurally invalid JSONL per RFC 7464**.
DW's `/v1/files` validator rejects with HTTP 500
`Internal server error` (the response body 21 bytes captured in
Slice 37 diagnostic).

**v30 "40/40 OK" paradox dissolved**: predated DW tightening
their validator. Probe uses `DoublewordProvider.prompt_only()`
which uses the SAME broken JSONL composition — both fail today.

**Fix shape** (minimum-diff structural):
1. `DoublewordProvider._compose_jsonl_batch_entry(entry: dict) ->
   str` (@staticmethod) — single source of truth for JSONL
   framing. Validates required fields (custom_id/method/url/body),
   validates body is dict, emits `json.dumps(entry) + "\n"`.
   Serialization params (ensure_ascii / separators) intentionally
   unchanged so any remaining DW issues are \n-independent.
2. Both raw `json.dumps(...)` call sites replaced.
3. Belt-and-braces guard in `_upload_file`: if payload doesn't
   end with \n, log structured WARNING and auto-append.
4. Slice 37 diagnostic parse now strips trailing \n before
   `json.loads`.

**Empirical proof on v33-shaped payload**:
- LEGACY:  357 bytes, last_byte=`}`,  ends_nl=False → DW HTTP 500
- SLICE38: 358 bytes, last_byte=`\n`, ends_nl=True  → expected accept
- DELTA: +1 byte — the exact structural fix DW asked for

**Test surface**: 13 new tests
(`tests/governance/test_slice38_jsonl_trailing_newline.py`):
- 5 AST pins (composer @staticmethod + signature + body+'\n' /
  both call sites use composer / guard wired)
- 8 spine (composer emits exactly ONE \n / parses as 1-record
  JSONL / TypeError on non-dict / ValueError on missing field
  / ValueError on non-dict body / byte shape preserved except
  \n / guard mentions composer / no raw json.dumps → _upload_file
  chain survives)

**Regression**: 173/173 green across Slices 20A→38.

**Next**: v34 capability detonation. First soak in 9 attempts
where the upstream rejection mechanism has been structurally
addressed at the canonical site.

**No euphoria**: this is the first time the root cause has been
*structurally addressed* — not the first time we *expected* to
fix capability. v34 will prove (or disprove) acceptance
empirically. If DW still returns 500 after \n is added, the
remaining diff investigation surfaces are: multipart Content-Type
(`application/jsonl` vs `application/json`), purpose field name
(`batch` vs other), Aegis bearer header set on /v1/files
specifically.

Composes [[project_slice_37_multipart_payload_cleanup]] +
[[project_v33_capability_soak_postmortem]].
