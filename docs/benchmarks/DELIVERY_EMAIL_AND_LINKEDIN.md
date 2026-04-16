# Delivery drafts — DW benchmarks report

These are drafts for the email + LinkedIn message that accompany the benchmark report. They're kept in the repo alongside the report so version-control captures the exact prose sent, and so future benchmark cycles can reuse the structure.

---

## Email draft

**To:** meryem@doubleword.ai *(verify address before sending)*
**Subject:** O+V × DoubleWord benchmarks — 11 days, 160+ sessions, honest write-up

---

Hi Meryem,

Promised write-up on how DoubleWord 397B and Gemma 4 31B have performed under Ouroboros + Venom's (O+V) battle tests. It's in the repo as a markdown file, PDF attached here for convenience.

**Full report:** https://github.com/drussell23/JARVIS-AI-Agent/blob/main/docs/benchmarks/DW_BENCHMARKS_2026-04-16.md
**PDF:** attached

Two things I want to surface up-front so the report doesn't read like a complaint thread:

1. **The reasoning quality on 397B is genuinely first-tier when generation completes.** The cost economics work exactly as DoubleWord promises. The callers we've mounted on Gemma 4 31B (`semantic_triage`, `ouroboros_plan`) are reliable at small payloads and are doing real work in the pipeline today.

2. **The primary blocker is a specific, reproducible failure mode on the streaming endpoint** — not a model or prompt-size issue. Two dated isolation tests (one on Gemma 31B, one on Qwen 397B) produced the identical `SSE stream stalled (no data for 30s)` signature on right-sized payloads. Section 3 of the report walks through both with session IDs, debug.log line numbers, and per-op latency data your gateway team can reproduce end-to-end.

Because of that one failure mode, we've had to topology-seal DW from every agent-generation route (IMMEDIATE, STANDARD, COMPLEX, BACKGROUND, SPECULATIVE) — which is why ~98% of our inference spend has ended up on Claude over the last 11 days despite a pipeline designed to put 397B at ~80% of ops. Section 8 lists concrete engineering asks framed as falsifiable items — the primary one is a diagnosis of the SSE behavior, the secondary is a stability contract on the non-streaming `/v1/chat/completions` endpoint (which O+V is building around in our "Functions, Not Agents" roadmap in Section 7).

Also worth flagging: the first end-to-end autonomous multi-file APPLY in the repo's history shipped on 2026-04-15 (session W, `bt-2026-04-15-230849`) — 4 Python test modules generated, validated, repaired, written to disk, autonomously committed, 20/20 pytest green. **It ran on Claude because DW was topology-sealed from COMPLEX.** That's the size of the work we'd like to route back to DoubleWord once the streaming piece is resolved.

Happy to share the full debug.logs for the two isolation tests (`bt-2026-04-14-182446` and `bt-2026-04-14-203740`) if a gateway engineer wants to do a 10-minute pairing read — they're too big for email but I can drop them in a shared folder.

Looking forward to the next meet — let me know what works on your side.

Best,
Derek

---

## LinkedIn message draft (2–3 sentences)

> Hi Meryem — finished the DW benchmark write-up from the last 11 days of O+V battle tests. Full report + session-level evidence in the repo, PDF just sent to your email. Headline: 397B reasoning quality is stellar, but we've isolated a reproducible SSE stream-stall signature that forced us to topology-seal DW from agent-generation routes — Section 8 has concrete asks framed for your gateway team.

---

## Notes on delivery

- **GitHub visibility:** the report is in a public repo, so the link Meryem receives is directly viewable without auth. No PR / no branch needed.
- **PDF attachment:** `DW_BENCHMARKS_2026-04-16.pdf` (~535 KB) renders cleanly on standard email clients. Generated via Chrome headless on pandoc-produced HTML (LaTeX not required).
- **Debug.log sharing:** the two isolation-test debug.logs total ~15–25 MB each; share via Google Drive / Dropbox / secure share rather than inline.
- **Signing:** sign off as "Derek" (the LinkedIn thread already uses first-name), not "Derek J. Russell."
- **Tone check before send:** the email opens with two "what DW does well" items before the blocker, and closes with the concrete ask + an offer of a collaborative 10-min pairing. That's the engineering-partner framing you asked for, not a complaint thread.
