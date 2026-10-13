# Engagement content format (STRICT — output is spliced by a script)

You are writing reader-engagement material for a long research paper:
`C:\Users\Jarvis\Desktop\TrinityAi\jarvis\docs\architecture\OV_RESEARCH_PAPER_2026-04-16.md`
(Ouroboros + Venom, an autonomous self-development engine). Read the line ranges you are assigned
in full before writing. Every question and answer must be supported by the paper's own text in that
range — never invent facts, names, numbers or behaviour. Cite the section (e.g. "See §9.").

For EACH unit assigned to you, write ONE file `<scratchpad>\engage\<key>.html` containing, in order:

1. N checkpoint blocks ("Pause and predict"), each placed BEFORE a `## §N.` heading inside the unit
   that is NOT the unit's first section. The question should make the reader predict or reason about
   what the upcoming section answers; the revealed answer gives the paper's answer.
2. One end-of-unit block.

## Hard syntax rules
- NO blank lines inside any HTML block. A block is one contiguous run of lines.
- The only Markdown line is the `#### Check your understanding: ...` heading, which has one blank
  line after it.
- Plain text inside tags. Use `<code>` for identifiers, `<strong>`/`<em>` sparingly.
- Escape `&` as `&amp;`, `<` as `&lt;`, `>` as `&gt;` in text. Never use a dollar sign (write
  "1.90 US dollars" or "zero cost").
- The marker comment lines (`<!-- CHECKPOINT before: ... -->`, `<!-- ENDBLOCK ... -->`,
  `<!-- END -->`) must appear exactly as shown. The `before:` text must be the EXACT full heading line
  copied from the paper, starting with `## §`.
- ids: lowercase, e.g. `part-iii`, `s92`, `s93`. Checkpoints numbered `cp-<key>-1`, `cp-<key>-2`.

## Checkpoint template
<!-- CHECKPOINT before: ## §9. CLASSIFY & ROUTE — Risk and Urgency -->
<aside class="checkpoint" id="cp-part-iii-1">
<p class="cp-label">Pause and predict</p>
<p class="cp-q">QUESTION (one or two sentences)</p>
<details class="cp-a"><summary>Reveal the answer</summary>
<p>ANSWER, 2-4 sentences. See §9.</p>
</details>
</aside>
<!-- END -->

## End-of-unit template (3 conceptual questions, then exactly 4 quiz questions)
<!-- ENDBLOCK part-iii -->
#### Check your understanding: Part III — The Eleven-Phase Pipeline

<div class="engage" id="engage-part-iii">
<p class="engage-kicker">Conceptual questions</p>
<details class="cq"><summary>1. QUESTION</summary>
<p>MODEL ANSWER, 3-6 sentences, explaining WHY, citing sections.</p>
</details>
<details class="cq"><summary>2. QUESTION</summary>
<p>...</p>
</details>
<details class="cq"><summary>3. QUESTION</summary>
<p>...</p>
</details>
<p class="engage-kicker">Quiz</p>
<div class="quiz" id="quiz-part-iii">
<div class="mcq" data-answer="2">
<p class="mcq-q">1. QUESTION</p>
<ol class="mcq-options" type="A">
<li>option A</li>
<li>option B</li>
<li>option C</li>
<li>option D</li>
</ol>
<details class="mcq-why"><summary>Show answer</summary>
<p><strong>B.</strong> EXPLANATION, 1-3 sentences, citing §n.</p>
</details>
</div>
(three more .mcq divs)
</div>
<p class="engage-kicker">Problem set</p>
<div class="pset" id="pset-part-iii">
<div class="problem">
<p class="problem-q"><strong>Problem 1 (trace).</strong> TASK, 2-5 sentences. A concrete exercise the reader works through.</p>
<details class="problem-hint"><summary>Hint</summary>
<p>One nudge, not the answer.</p>
</details>
<details class="problem-sol"><summary>Worked solution</summary>
<p>Step-by-step solution, 4-10 sentences; several p tags allowed (still no blank lines). Cite sections.</p>
</details>
</div>
(two more .problem divs)
</div>
</div>
<!-- END -->

## Problem set guidance (exactly 3 problems per unit)
Each unit gets three practice problems of different kinds, labelled in the bold lead-in:
- **(trace)** follow a concrete scenario through the mechanism (e.g. "a test fails at 03:00; walk the
  signal from sensor to commit, naming each phase and what could stop it").
- **(design)** propose or critique a change and reason about what the architecture's gates, budgets or
  trade-offs would do to it.
- **(code reading)** send the reader to a real file the paper cites, with a concrete thing to find.
  ONLY use a path if it exists in the repo today — verify with Glob/Read under
  `C:\Users\Jarvis\Desktop\TrinityAi\jarvis\`. If you cannot verify one, use a **(analysis)** problem
  instead (reason from a table, figure or number in the paper).
Solutions must be derivable from the paper (plus, for code reading, the file you verified).

`data-answer` is the 1-based index of the correct option and MUST match the letter in the explanation.

## Quality bar
- Conceptual questions test understanding: why a design choice was made, what trade-off it
  carries, what would break without it, how two mechanisms relate. Not trivia lookups.
- Quiz: mix of recall and reasoning; four plausible options of similar length; exactly one correct;
  vary the position of the correct answer across questions (do not always use B).
- Write for a smart reader new to the system: plain, direct English, short sentences, no hype.
- Where the paper itself reports a limitation or negative result, it is fair game — honesty is a
  feature of this paper.
- Before finishing, re-read your file and check: every `before:` heading exists verbatim in the
  paper; every data-answer matches its explanation letter; no blank lines inside blocks; no `$`.
Reply with just the list of files written and the checkpoint headings used.
