# ADVANCED tier format (STRICT — spliced by a script)

Same paper, same hard syntax rules as FORMAT.md (read it first:
`<scratchpad>\engage\FORMAT.md`): no blank lines inside the block, escape `&` `<` `>`, never a
dollar sign, plain HTML only, every claim supported by the paper (or by a repo file you verified).
Also read the unit's existing standard file `<scratchpad>\engage\<key>.html` so you do NOT repeat
any question already asked there.

For each unit, write `<scratchpad>\engage\<key>-adv.html` containing exactly one block:

<!-- ADVBLOCK part-iii -->
<p class="engage-kicker engage-kicker-adv">Advanced conceptual questions</p>
<details class="cq-adv"><summary>A1. QUESTION</summary>
<p>MODEL ANSWER, 5-10 sentences; several p tags allowed.</p>
</details>
<details class="cq-adv"><summary>A2. QUESTION</summary>
<p>...</p>
</details>
<p class="engage-kicker engage-kicker-adv">Challenge problems</p>
<div class="pset pset-adv">
<div class="problem problem-adv">
<p class="problem-q"><strong>Challenge 1 (KIND).</strong> TASK, 3-8 sentences, with concrete givens.</p>
<details class="problem-hint"><summary>Hint</summary>
<p>One nudge.</p>
</details>
<details class="problem-sol"><summary>Worked solution</summary>
<p>Full solution, 8-20 sentences across several p tags (a ul/ol is allowed). State assumptions,
show the reasoning step by step, and end with the conclusion and how to check it.</p>
</details>
</div>
<div class="problem problem-adv">
<p class="problem-q"><strong>Challenge 2 (KIND).</strong> ...</p>
(hint + worked solution as above)
</div>
</div>
<!-- END -->

## What "advanced" means here
These are for a reader who has mastered the standard questions. Each must require combining ideas,
not recalling one.
- Advanced conceptual questions: synthesis across sections or Parts (cite both), second-order
  effects, failure analysis ("what happens if X and Y happen at once?"), or a trade-off the paper
  leaves open where the answer argues a position and names the evidence that would settle it.
- Challenge problem KINDs (use two different kinds per unit):
  - **adversarial**: construct an input, candidate or event sequence that tries to defeat a gate or
    invariant, then determine which layer stops it, or show honestly that the paper's design would
    not stop it and what would.
  - **systems design**: extend or redesign a mechanism under stated constraints (budget, latency,
    one GPU, no paid lanes), and justify against the paper's principles and gates.
  - **quantitative**: a calculation from numbers the paper gives (budgets, rates, counts, timings),
    with the arithmetic shown.
  - **cross-Part trace**: follow one scenario through mechanisms from at least two Parts.
  - **research**: frame a measurable experiment (hypothesis, metric, control, what result would
    falsify it) for an open question the paper names.
- Hard but fair: the solution must be derivable from the paper. Do not invent mechanisms; when a
  problem asks the reader to propose something new, label the proposal as such in the solution.
- Do not reuse the standard file's questions.
Before finishing, re-read and verify the rules. Reply with just the file names written.
