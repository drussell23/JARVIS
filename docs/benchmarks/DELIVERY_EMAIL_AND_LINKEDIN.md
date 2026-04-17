# Delivery drafts — DW benchmarks report

These are drafts for the email + LinkedIn message that accompany the benchmark report. They're kept in the repo alongside the report so version-control captures the exact prose sent, and so future benchmark cycles can reuse the structure.

---

## Email draft

**To:** meryem@doubleword.ai *(verify address before sending)*
**Subject:** O+V × DoubleWord — 11-day battle-test benchmarks + the opportunity I think we're sitting on

---

Hi Meryem,

Promised write-up on how DoubleWord 397B and Gemma 4 31B have performed under Ouroboros + Venom's (O+V) battle tests over the last 11 days. Full report is in the repo as markdown, and I've attached the PDF to this email for convenience.

**Full report:** https://github.com/drussell23/JARVIS-AI-Agent/blob/main/docs/benchmarks/DW_BENCHMARKS_2026-04-16.md
**PDF:** attached (~47 pages, written to work for both the business and engineering sides of your team)

I want to flag three things up-front so the report lands in the right frame:

**1. This is written as an engineering-partner document, not a complaint thread.**
The tone throughout is what we've actually observed + what I think we can build together. Every major section opens with a plain-English "big picture" paragraph (targeted at anyone on the business side), then goes into technical depth for your gateway/infra team. A glossary is included in Appendix A for readers new to autonomous-AI systems.

**2. The 397B reasoning quality and the pricing model are both genuinely first-tier.**
Part III walks through four specific strengths we've observed with debug-log citations. The Gemma 4 31B + 397B MoE combo at your price point is structurally unique in the market today — no one else offers it. I want to make that part of the record before anything else.

**3. The Apr 14 stall signature did not reproduce on Apr 16 in dated standalone smoke tests — including at agent-scale payloads matching the original isolation tests.**
Part IV is a detailed walkthrough of two isolation tests on 2026-04-14 — one on Gemma 31B (BACKGROUND, 0/13), one on Qwen 397B (STANDARD, 0/3) — both showing the same `SSE stream stalled (no data for 30s)` signature. **As a live follow-up before finalizing this report, I ran four dated smoke-test reproductions on 2026-04-16: small and agent-scale payloads on both models. All four streaming probes completed cleanly**, including a 258-second Qwen 397B run with 3,798 SSE chunks at the exact payload scale Apr 14 failed on. That's genuinely encouraging news about the state of your streaming infrastructure. The honest interpretation is nuanced: the Apr 14 observations were real when we observed them; they may have been resolved since, may be intermittent, or may require sustained concurrent load / specific production-pipeline context to trigger. Full four-run addendum in §3.4 with side-by-side metrics; what we did and didn't test in §14.5; refined hypothesis list in §25.2. The natural next step is a pairing session with your gateway team to understand the discrepancy — and we've outlined a staged route-by-route re-engagement plan that flips one YAML flag per route as each clears shadow telemetry.

**Part VI is the part I most want your team to read.** It's the commercial opportunity I think DoubleWord is sitting on: the per-token cost delta vs Claude (~36× on Sonnet, ~178× on Opus), what that means for autonomous-AI startups, and why the timing window to own this category is now. If the SSE story gets resolved and Phase 0 of our "Functions, Not Agents" reseating (Section 22-24) delivers clean shadow telemetry, the case for DoubleWord as the default inference provider for autonomy builders writes itself.

Also worth flagging for full transparency: **the first end-to-end autonomous multi-file APPLY in O+V's history shipped on 2026-04-15** — four Python test modules autonomously generated, validated, repaired, written to disk, committed, 20/20 pytest green. That milestone ran on Claude because DoubleWord was topology-sealed from the COMPLEX route at the time. That's the size of work available to re-route to DoubleWord once the streaming piece lands — not hypothetical, reproducible today.

Happy to share the two isolation-test debug logs for a 30-minute pairing with whoever on your gateway team wants to look at them live. They're too large for email but I can drop them into a shared folder.

Looking forward to the next meet — let me know what works on your side.

Best,
Derek

---

## LinkedIn message draft (2–3 sentences)

> Hi Meryem — finished the DW benchmark write-up from the last 11 days of O+V battle tests. Full report is in the repo (47-page PDF also attached to the email I just sent), written to work for both the business and engineering sides of your team. Quick headline: 397B reasoning quality + your pricing are both already first-tier; the report walks through one specific streaming-transport behavior we've isolated, plus what I think is a significant commercial opportunity if it gets resolved (Part VI has the numbers).

---

## Notes on delivery

- **GitHub visibility:** the report is in a public repo, so the link Meryem receives is directly viewable without auth. No PR / no branch needed.
- **PDF attachment:** `DW_BENCHMARKS_2026-04-16.pdf` (~1.4 MB, 47 pages) renders cleanly on standard email clients. Generated via Chrome headless on pandoc-produced HTML with a custom print-ready CSS. LaTeX not required.
- **Debug.log sharing:** the two isolation-test debug.logs total ~15–25 MB each; share via Google Drive / Dropbox / secure share rather than inline.
- **Signing:** sign off as "Derek" (the LinkedIn thread already uses first-name), not "Derek J. Russell."
- **Tone check before send:**
  - Opens with four strengths (Part III) before discussing any blocker (Part IV).
  - Every blocker paragraph pairs the technical detail with a plain-English analogy.
  - The commercial opportunity section (Part VI) explicitly frames fixing the SSE issue as DoubleWord's path to becoming "the default inference provider for autonomous AI workloads."
  - Every engineering ask (Part VIII) is framed as a collaborative invitation with hypotheses to test, not a list of demands.
  - No condescension, no passive-aggression, no implicit "you should have done this" subtext anywhere in the document.

---

## Version log

- **v6 (2026-04-16 late evening, final)** — Final presentation-grade polish. Figures redesigned for readability: 300 DPI, wider layouts, larger fonts (12pt axis labels, 14pt titles), consistent restrained palette, fewer elements per chart, one headline insight per figure. Full PDF outline/bookmark tree added via pypdf post-processor — Preview's sidebar "Table of Contents" view now shows a 96-node navigable hierarchy that maps 1:1 to section structure. Clickable TOC entries inside the PDF body also work (212 named destinations preserved). 64-page PDF, ~3 MB.
- **v5 (2026-04-16 evening)** — Research-paper presentation upgrade. Six matplotlib-generated figures embedded at their narrative-relevant sections (smoke-test timeline, Qwen SSE chunk profile, Qwen reasoning-token composition, inference-spend split, per-op cost comparison, scaling economics). Manual TOC replaced with pandoc-generated navigable TOC containing 213 clickable anchor destinations — click any section in the PDF TOC to jump directly. Running header on every page, page-X-of-Y footer. Serif body typography (Charter/Georgia) for readability, sans display (SF Pro) for headings. 47-page PDF, 2.52 MB.
- **v4 (2026-04-16 evening)** — Agent-scale smoke test retest integrated. Four total runs now: small + agent-scale, both models. All streaming probes succeeded, including 258s Qwen 397B at agent scale. §3.4 expanded with side-by-side metrics for all 4 runs. §14.5 reframed as "signature did not reproduce." §25.2 hypotheses refined — two hypotheses ruled out, four remain consistent. Email draft and TL;DR shifted to acknowledge the genuinely positive finding while maintaining honest nuance.
- **v3 (2026-04-16 evening)** — Apr 16 smoke-test addendum integrated: §3.4 (dated follow-up reproduction with side-by-side metrics), §14.5 (important payload-scale nuance), §25.2 (refined hypothesis list). 52-page PDF, 1.57 MB.
- **v2 (2026-04-16 afternoon)** — full rewrite with table of contents, dual plain-English + technical framing, new "Opportunity" section (Part VI) with unit-economics numbers, glossary appendix, professional CSS with print/PDF optimization, 47-page PDF.
- **v1 (2026-04-16 morning)** — initial draft, 10 sections, session ladder + cost telemetry.
