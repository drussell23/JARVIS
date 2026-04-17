# Delivery drafts — DW benchmarks report

These are drafts for the email + LinkedIn message that accompany the benchmark report. They're kept in the repo alongside the report so version-control captures the exact prose sent, and so future benchmark cycles can reuse the structure.

---

## Email draft (v6, final send version)

**To:** meryem@doubleword.ai *(verify address before sending)*
**Subject:** O+V × DoubleWord — 11-day battle-test benchmarks + the opportunity I think we're sitting on

---

Hi Meryem,

Please find the promised write-up on how DoubleWord 397B and Gemma 4 31B have performed under Ouroboros + Venom's (O+V) battle tests over the last 11 days. The full report is available in the repo and I've attached a PDF version for your convenience.

**Full report:** https://drussell23.github.io/JARVIS/benchmarks/DW_BENCHMARKS_2026-04-16.html
**PDF:** attached to this email

I want to flag three key items up-front to frame the report correctly:

**1. Collaboration tone.** This is intended as an engineering-partner document rather than a list of complaints. It reflects our actual observations and what I believe we can build together. Each major section begins with a "big picture" paragraph for business context, followed by technical depth for your infrastructure team. A glossary is included as Appendix A.

**2. Market position.** The 397B reasoning quality and pricing model are genuinely first-tier. Part III outlines four specific strengths we observed, each cited to debug-log evidence. The combination of Gemma 4 31B and 397B MoE at your current price point — **~36× cheaper than Claude Sonnet, ~178× cheaper than Opus** for equivalent work — is structurally unique in the market, and I wanted that noted before anything else.

**3. Streaming infrastructure — important nuance.** The stall signature we observed on April 14 did not reproduce during our standalone smoke tests today, April 16, even at agent-scale payloads. Part IV details the initial isolation tests. My follow-up runs on April 16 showed **all four streaming probes completing cleanly — including a 258-second Qwen 397B agent-scale stream that emitted 3,798 SSE chunks with zero stalls**. The April 14 observations were real, but the current state is genuinely encouraging — the issues may have been resolved or may be intermittent. Full addendum in §3.4, refined hypothesis list in §25.2. I'd suggest a pairing session with your gateway team to understand the discrepancy and discuss the staged re-engagement plan in §14.5.

I'd also like to highlight Part VI, which discusses the commercial opportunity for DoubleWord. Given the per-token cost savings, there's a significant window to become the default provider for autonomy builders industry-wide if the SSE stability is finalized.

For full transparency: we achieved a major milestone on April 15 — the first end-to-end autonomous multi-file APPLY in the repo's history (Session W, `bt-2026-04-15-230849`). Four Python test modules autonomously generated, validated, repaired, written to disk, committed, 20/20 pytest green. It ran on Claude only because DoubleWord was topology-sealed at the time — exactly the type of workload we're ready to re-route to DoubleWord once the streaming piece is confirmed.

I'm happy to share the isolation-test debug logs via a shared folder if someone from your gateway team would like to review them during a 30-minute pairing session.

Looking forward to our next meeting — let me know what day and time works best on your side.

Best,
Derek

---

## LinkedIn message draft (2–3 sentences)

> Hi Meryem — finished the DW benchmark write-up from the last 11 days of O+V battle tests. Full report is live at https://drussell23.github.io/JARVIS/benchmarks/DW_BENCHMARKS_2026-04-16.html (PDF also attached to the email I just sent), written to work for both the business and engineering sides of your team. Quick headline: 397B reasoning quality + your pricing are both already first-tier; the April 14 stall signature didn't reproduce on today's standalone smoke tests (including a clean 258-second Qwen 397B agent-scale run) — so the picture's more encouraging than the initial report framing suggested. Part VI has the commercial opportunity numbers.

---

## Notes on delivery

- **GitHub Pages URL:** `https://drussell23.github.io/JARVIS/benchmarks/DW_BENCHMARKS_2026-04-16.html` — renders the HTML with figures, clickable TOC, embedded styling. Pages source is `main` branch + `/docs` folder (pre-existing config from the voice.ai report), so the URL drops the `/docs` prefix — the `/docs/` in the repo path maps to the site root. `.nojekyll` is committed at `docs/.nojekyll` (inside the Pages source) to disable Jekyll processing on the served tree.
- **PDF attachment:** `DW_BENCHMARKS_2026-04-16.pdf` (~3 MB, 64 pages) renders cleanly on standard email clients. Has a navigable sidebar outline (96 bookmark nodes) and clickable in-body TOC (212 named destinations). Generated via pandoc → Chrome headless → pypdf outline post-processor.
- **Debug.log sharing:** the two isolation-test debug.logs total ~15–25 MB each; share via Google Drive / Dropbox / secure share rather than inline.
- **Signing:** sign off as "Derek" (the LinkedIn thread already uses first-name), not "Derek J. Russell."
- **Tone check before send:**
  - Opens with market position + strengths before discussing any blocker.
  - The Apr 16 follow-up reframes the story from "streaming is broken" to "streaming appears healthy today; let's understand the Apr 14 discrepancy together."
  - The commercial opportunity section (Part VI) explicitly frames fixing the SSE issue as DoubleWord's path to becoming "the default inference provider for autonomous AI workloads."
  - Every engineering ask (Part VIII) is framed as a collaborative invitation with hypotheses to test, not a list of demands.
  - No condescension, no passive-aggression, no implicit "you should have done this" subtext anywhere in the document.

---

## Version log

- **v7 (2026-04-16 late evening)** — Email draft updated to the final send version: `Hi Meryem` (matches LinkedIn thread tone), GitHub Pages URL (`drussell23.github.io/JARVIS/...`), concrete numbers added in items 2 and 3 (36×/178× cost ratios, 258s/3,798 chunks stream detail), Session W ID cited in the transparency paragraph. LinkedIn draft similarly updated. `.nojekyll` committed at repo root to make Pages serve the `_report_style.css` correctly.
- **v6 (2026-04-16 late evening)** — Final presentation-grade polish. Figures redesigned for readability: 300 DPI, wider layouts, larger fonts (12pt axis labels, 14pt titles), consistent restrained palette, fewer elements per chart, one headline insight per figure. Full PDF outline/bookmark tree added via pypdf post-processor — Preview's sidebar "Table of Contents" view now shows a 96-node navigable hierarchy that maps 1:1 to section structure. Clickable TOC entries inside the PDF body also work (212 named destinations preserved). 64-page PDF, ~3 MB.
- **v5 (2026-04-16 evening)** — Research-paper presentation upgrade. Six matplotlib-generated figures embedded at their narrative-relevant sections (smoke-test timeline, Qwen SSE chunk profile, Qwen reasoning-token composition, inference-spend split, per-op cost comparison, scaling economics). Manual TOC replaced with pandoc-generated navigable TOC containing 213 clickable anchor destinations — click any section in the PDF TOC to jump directly. Running header on every page, page-X-of-Y footer. Serif body typography (Charter/Georgia) for readability, sans display (SF Pro) for headings. 47-page PDF, 2.52 MB.
- **v4 (2026-04-16 evening)** — Agent-scale smoke test retest integrated. Four total runs now: small + agent-scale, both models. All streaming probes succeeded, including 258s Qwen 397B at agent scale. §3.4 expanded with side-by-side metrics for all 4 runs. §14.5 reframed as "signature did not reproduce." §25.2 hypotheses refined — two hypotheses ruled out, four remain consistent. Email draft and TL;DR shifted to acknowledge the genuinely positive finding while maintaining honest nuance.
- **v3 (2026-04-16 evening)** — Apr 16 smoke-test addendum integrated: §3.4 (dated follow-up reproduction with side-by-side metrics), §14.5 (important payload-scale nuance), §25.2 (refined hypothesis list). 52-page PDF, 1.57 MB.
- **v2 (2026-04-16 afternoon)** — full rewrite with table of contents, dual plain-English + technical framing, new "Opportunity" section (Part VI) with unit-economics numbers, glossary appendix, professional CSS with print/PDF optimization, 47-page PDF.
- **v1 (2026-04-16 morning)** — initial draft, 10 sections, session ladder + cost telemetry.
