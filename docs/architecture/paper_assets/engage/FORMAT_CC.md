# Per-section QUICK CHECKS and the CONNECTIONS tier (STRICT — spliced by a script)

Read `FORMAT.md` in this folder first: the same hard syntax rules apply (no blank lines inside a
block, plain HTML, escape `&` `<` `>` in text, never a dollar sign, every claim supported by the paper).
The paper is `docs/architecture/OV_RESEARCH_PAPER_2026-04-16.md`. It contains already-spliced blocks
between `<!-- engage:begin -->` / `<!-- engage:end -->` and an Appendix G; ignore those when reading.
Also skim the unit's existing files (`<key>.html`, `<key>-adv.html`) so you do not repeat a question.

You write TWO files per unit, into `docs/architecture/paper_assets/engage/`:

## 1. `<key>-cc.html` — one Quick check per section

One block for EVERY `## §N.` section in the unit. For the addenda units (`s92`, `s93`), one block for
EVERY `### §92.N` / `### §93.N` subsection instead. Block template:

<!-- QUICKCHECK after: ## §9. CLASSIFY & ROUTE — Risk and Urgency -->
<div class="quick-check" id="qc-9">
<p class="qc-label">Quick check · §9</p>
<details class="qc"><summary>1. QUESTION</summary>
<p>ANSWER, 1-3 sentences.</p>
</details>
<details class="qc"><summary>2. QUESTION</summary>
<p>ANSWER.</p>
</details>
</div>
<!-- END -->

- `after:` is the EXACT full heading line of the section, copied from the paper (raw text, e.g. `&`
  not `&amp;`). ids: `qc-9` for §9, `qc-93-4` for §93.4.
- 2-3 questions per section (1-2 if the section is under ~15 lines).
- Comprehension, not trivia: what the mechanism does, why it exists, what it would break if removed,
  how a small detail (a threshold, an ordering, a flag default) serves the design. Each must be
  answerable from THAT section.
- Answers are short and precise; cite a subsection or table when useful.

## 2. `<key>-x.html` — the Connections tier (0 to 3 problems)

Problems that apply a real subject to a mechanism in this unit. Allowed subjects, used ONLY where the
subject genuinely models the mechanism:
physics of hardware / computer architecture (memory bandwidth, roofline, VRAM and KV-cache arithmetic,
latency), queueing theory (Little's law, utilization, throughput), probability and statistics
(confidence intervals, hypothesis tests, base rates), information theory (entropy, information gain),
control theory (feedback, hysteresis, EWMA filters, stability), graph theory (DAGs, ordering, cycles),
formal logic and verification (invariants, SMT), economics (cost, marginal cost, budgets),
thermodynamics (only as an explicitly examined analogy, e.g. the paper's "thermodynamic containment"
framing: where it holds and where it breaks), and ADVANCED MATHEMATICS where the system really uses
it: linear algebra (LoRA as low-rank factorisation W + BA and its parameter count; embeddings, cosine
similarity, centroids and k-means; attention and KV-cache as matrix products), optimisation and
calculus (gradients, gradient norm, advantage normalisation in GRPO), Markov chains and stochastic
processes (the Wang RSI formulation the paper cites; retry ladders as absorbing chains),
combinatorics (MoE top-k expert routing), numerical analysis (4-bit quantisation error).
Up to THREE Connections problems per unit; prefer variety across subjects.
If no subject genuinely fits the unit, write the block with zero problems (just the kicker line is
NOT allowed; write `<!-- XBLOCK key -->` then `<!-- NONE -->` then `<!-- END -->`).

Template:

<!-- XBLOCK part-iii -->
<p class="engage-kicker engage-kicker-x">Connections</p>
<div class="pset pset-x">
<div class="problem problem-x">
<p class="problem-q"><strong>Connection 1 (Queueing theory).</strong> TASK, 3-8 sentences, concrete givens.</p>
<details class="problem-hint"><summary>Hint</summary>
<p>One nudge.</p>
</details>
<details class="problem-sol"><summary>Worked solution</summary>
<p>Full solution, 6-15 sentences across several p tags, with the arithmetic shown, ending with what the
result says about the architecture.</p>
</details>
</div>
</div>
<!-- END -->

Rules for numbers: use the paper's numbers. A number NOT in the paper (for example an RTX 5090's
memory bandwidth, about 1.8 TB/s) may be used only if it is a widely published specification, and the
problem must state it explicitly as a given assumption. Never present a derived estimate as a
measurement. Physics must be real physics, not metaphor dressed as calculation.

Before finishing, verify: every `after:` heading exists verbatim; no blank lines inside blocks; no `$`.
Reply with the files written and, per unit, the subjects used in the Connections tier.
