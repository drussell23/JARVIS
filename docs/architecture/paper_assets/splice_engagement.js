// Splice engagement blocks into the O+V paper and build Appendix G (answer key).
// Usage: node splice.js <paper.md> <engageDir>   (idempotent: strips prior splices first)
"use strict";
const fs = require("fs");
const path = require("path");

const [paperPath, dir] = process.argv.slice(2);
const UNITS = [
  ["part-i", /^# PART II —/], ["part-ii", /^# PART III —/], ["part-iii", /^# PART IV —/],
  ["part-iv", /^# PART V —/], ["part-v", /^# PART VI —/], ["part-vi", /^# PART VII —/],
  ["part-vii", /^# PART VIII —/], ["part-viii", /^# PART IX —/], ["part-ix", /^# PART X —/],
  ["part-x", /^# PART XI —/], ["part-xi", /^# PART XII —/], ["part-xii", /^# PART XIII —/],
  ["part-xiii", /^# PART XIV —/], ["part-xiv", /^# PART XV —/], ["part-xv", /^## §92\. /],
  ["s92", /^## §93\. /], ["s93", /^## Appendix A — /],
];

let text = fs.readFileSync(paperPath, "utf8").replace(/\r\n/g, "\n");
// Strip previous runs.
text = text.replace(/\n*<!-- engage:begin -->[\s\S]*?<!-- engage:end -->\n*/g, "\n\n");
text = text.replace(/\n*<!-- answer-key:begin -->[\s\S]*?<!-- answer-key:end -->\n*/g, "\n\n");
let lines = text.split("\n");

const errors = [];
function insertionPoint(idx) {
  // Walk back over blank lines and one horizontal rule so the block sits before the separator.
  let i = idx;
  while (i > 0 && lines[i - 1].trim() === "") i--;
  if (i > 0 && lines[i - 1].trim() === "---") {
    i--;
    while (i > 0 && lines[i - 1].trim() === "") i--;
  }
  return i;
}
function insertAt(i, block) {
  lines.splice(i, 0, "", "<!-- engage:begin -->", ...block.split("\n"), "<!-- engage:end -->", "");
}

const strip = (s) => s.replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
const all = (re, s) => { const out = []; let m; const r = new RegExp(re.source, re.flags.includes("g") ? re.flags : re.flags + "g"); while ((m = r.exec(s))) out.push(m); return out; };
const esc = (s) => s; // content is already HTML-escaped by the format contract

// Pass 1: per-section Quick checks. They go first so that a checkpoint for the NEXT section, and a
// Part's end block, land after the last section's Quick check.
function headingLevel(l) { const m = l.match(/^(#{1,6}) /); return m ? m[1].length : 0; }
function sectionEnd(idx) {
  // The line index of the next heading at the same or a higher level, skipping fenced code.
  const lvl = headingLevel(lines[idx]);
  let fence = false;
  for (let i = idx + 1; i < lines.length; i++) {
    if (/^\s*(```|~~~)/.test(lines[i])) { fence = !fence; continue; }
    if (fence) continue;
    if (lines[i].startsWith("<!-- answer-key:begin")) return i;
    const h = headingLevel(lines[i]);
    if (h && h <= lvl) return i;
  }
  return lines.length;
}
const qcByUnit = {};
for (const [key] of UNITS) {
  const file = path.join(dir, key + "-cc.html");
  if (!fs.existsSync(file)) { if (process.env.REQUIRE_CC) errors.push(`missing ${key}-cc.html`); continue; }
  const src = fs.readFileSync(file, "utf8").replace(/\r\n/g, "\n");
  if (/\$/.test(src)) errors.push(`${key}-cc: contains a dollar sign`);
  qcByUnit[key] = [];
  for (const m of all(/<!-- QUICKCHECK after: (.+?) -->\n([\s\S]*?)\n<!-- END -->/g, src)) {
    const heading = m[1].trim();
    const idx = lines.findIndex((l) => l.trim() === heading);
    if (idx < 0) { errors.push(`${key}-cc: heading not found: ${heading}`); continue; }
    if (/\n\s*\n/.test(m[2])) errors.push(`${key}-cc: blank line inside block for ${heading}`);
    const qs = all(/<details class="qc"><summary>([\s\S]*?)<\/summary>([\s\S]*?)<\/details>/g, m[2])
      .map((q) => ({ q: q[1], a: q[2].trim() }));
    if (qs.length < 1 || qs.length > 3) errors.push(`${key}-cc: ${qs.length} questions for ${heading}`);
    const label = strip((m[2].match(/<p class="qc-label">([\s\S]*?)<\/p>/) || [])[1] || heading);
    insertAt(insertionPoint(sectionEnd(idx)), m[2]);
    qcByUnit[key].push({ label, qs });
  }
}

const keyUnits = [];
for (const [key, boundary] of UNITS) {
  const file = path.join(dir, key + ".html");
  if (!fs.existsSync(file)) { errors.push(`missing ${key}.html`); continue; }
  const src = fs.readFileSync(file, "utf8").replace(/\r\n/g, "\n");
  if (/\$/.test(src)) errors.push(`${key}: contains a dollar sign`);

  // Checkpoints
  const cps = all(/<!-- CHECKPOINT before: (.+?) -->\n([\s\S]*?)\n<!-- END -->/g, src);
  const cpKey = [];
  for (const m of cps) {
    const heading = m[1].trim();
    const idx = lines.findIndex((l) => l.trim() === heading);
    if (idx < 0) { errors.push(`${key}: checkpoint heading not found: ${heading}`); continue; }
    if (/\n\s*\n/.test(m[2])) errors.push(`${key}: blank line inside checkpoint`);
    insertAt(insertionPoint(idx), m[2]);
    const q = (m[2].match(/<p class="cp-q">([\s\S]*?)<\/p>/) || [])[1] || "";
    const a = (m[2].match(/<details class="cp-a">[\s\S]*?<\/summary>([\s\S]*?)<\/details>/) || [])[1] || "";
    cpKey.push({ q, a: a.trim() });
  }

  // End block
  const eb = src.match(/<!-- ENDBLOCK [^>]*-->\n([\s\S]*?)\n<!-- END -->/);
  if (!eb) { errors.push(`${key}: no ENDBLOCK`); continue; }
  let body = eb[1];
  // Advanced tier: spliced inside the unit's .engage container, after the standard problem set.
  let adv = null;
  const advFile = path.join(dir, key + "-adv.html");
  if (fs.existsSync(advFile)) {
    const advSrc = fs.readFileSync(advFile, "utf8").replace(/\r\n/g, "\n");
    if (/\$/.test(advSrc)) errors.push(`${key}-adv: contains a dollar sign`);
    const am = advSrc.match(/<!-- ADVBLOCK [^>]*-->\n([\s\S]*?)\n<!-- END -->/);
    if (!am) errors.push(`${key}-adv: no ADVBLOCK`);
    else {
      adv = am[1];
      if (/\n\s*\n/.test(adv)) errors.push(`${key}-adv: blank line inside block`);
      const bl = body.split("\n");
      const last = bl.length - 1 - [...bl].reverse().findIndex((l) => l.trim() === "</div>");
      bl.splice(last, 0, adv);
      body = bl.join("\n");
    }
  } else if (process.env.REQUIRE_ADV) errors.push(`missing ${key}-adv.html`);
  // Connections tier: after the advanced tier, still inside .engage. A unit may have none.
  let xb = null;
  const xFile = path.join(dir, key + "-x.html");
  if (fs.existsSync(xFile)) {
    const xSrc = fs.readFileSync(xFile, "utf8").replace(/\r\n/g, "\n");
    if (/\$/.test(xSrc)) errors.push(`${key}-x: contains a dollar sign`);
    const xm = xSrc.match(/<!-- XBLOCK [^>]*-->\n([\s\S]*?)\n?<!-- END -->/);
    if (!xm) errors.push(`${key}-x: no XBLOCK`);
    else if (!/<!-- NONE -->/.test(xm[1])) {
      xb = xm[1];
      if (/\n\s*\n/.test(xb)) errors.push(`${key}-x: blank line inside block`);
      const bl = body.split("\n");
      const last = bl.length - 1 - [...bl].reverse().findIndex((l) => l.trim() === "</div>");
      bl.splice(last, 0, xb);
      body = bl.join("\n");
    }
  } else if (process.env.REQUIRE_CC) errors.push(`missing ${key}-x.html`);
  const title = ((body.match(/^#### Check your understanding: (.+)$/m) || [])[1] || key).trim();
  const html = body.replace(/^#### .*\n\n?/, "");
  if (/\n\s*\n/.test(html)) errors.push(`${key}: blank line inside end block`);
  const bIdx = lines.findIndex((l) => boundary.test(l));
  if (bIdx < 0) { errors.push(`${key}: boundary not found`); continue; }
  insertAt(insertionPoint(bIdx), body);

  // Collect answer key material
  const cqs = all(/<details class="cq"><summary>([\s\S]*?)<\/summary>([\s\S]*?)<\/details>/g, html)
    .map((m) => ({ q: m[1], a: m[2].trim() }));
  const mcqs = all(/<div class="mcq" data-answer="(\d+)">([\s\S]*?)<details class="mcq-why">[\s\S]*?<\/summary>([\s\S]*?)<\/details>\s*<\/div>/g, html)
    .map((m) => {
      const opts = all(/<li>([\s\S]*?)<\/li>/g, m[2]).map((o) => o[1]);
      const n = parseInt(m[1], 10);
      const expl = m[3].trim();
      const letterInExpl = (strip(expl).match(/^([A-D])\./) || [])[1];
      const L = String.fromCharCode(64 + n);
      if (!(n >= 1 && n <= opts.length)) errors.push(`${key}: data-answer ${n} out of range`);
      if (letterInExpl && letterInExpl !== L) errors.push(`${key}: data-answer ${L} but explanation says ${letterInExpl}: ${strip(m[2]).slice(0, 60)}`);
      return { q: (m[2].match(/<p class="mcq-q">([\s\S]*?)<\/p>/) || [])[1] || "", expl, opts: opts.length };
    });
  const stdHtml = [adv, xb].reduce((h, b) => (b ? h.replace(b, "") : h), html);
  const xProbs = xb ? all(/<p class="problem-q">([\s\S]*?)<\/p>[\s\S]*?<details class="problem-sol">[\s\S]*?<\/summary>([\s\S]*?)<\/details>/g, xb)
    .map((m) => ({ q: m[1], a: m[2].trim() })) : [];
  if (xb && (xProbs.length < 1 || xProbs.length > 3)) errors.push(`${key}-x: ${xProbs.length} connection problems (want 0-3)`);
  const probs = all(/<p class="problem-q">([\s\S]*?)<\/p>[\s\S]*?<details class="problem-sol">[\s\S]*?<\/summary>([\s\S]*?)<\/details>/g, stdHtml)
    .map((m) => ({ q: m[1], a: m[2].trim() }));
  const advCqs = adv ? all(/<details class="cq-adv"><summary>([\s\S]*?)<\/summary>([\s\S]*?)<\/details>/g, adv)
    .map((m) => ({ q: m[1], a: m[2].trim() })) : [];
  const advProbs = adv ? all(/<p class="problem-q">([\s\S]*?)<\/p>[\s\S]*?<details class="problem-sol">[\s\S]*?<\/summary>([\s\S]*?)<\/details>/g, adv)
    .map((m) => ({ q: m[1], a: m[2].trim() })) : [];
  if (adv && (advCqs.length < 2 || advCqs.length > 3)) errors.push(`${key}-adv: ${advCqs.length} advanced questions (want 2-3)`);
  if (adv && (advProbs.length < 2 || advProbs.length > 3)) errors.push(`${key}-adv: ${advProbs.length} challenge problems (want 2-3)`);
  if (cqs.length !== 3) errors.push(`${key}: ${cqs.length} conceptual questions (want 3)`);
  if (mcqs.length !== 4) errors.push(`${key}: ${mcqs.length} quiz questions (want 4)`);
  if (probs.length !== 3) errors.push(`${key}: ${probs.length} problems (want 3)`);
  keyUnits.push({ key, title, cpKey, cqs, mcqs, probs, advCqs, advProbs, xProbs, qcs: qcByUnit[key] || [] });
}

// Appendix G — placed before the closing "Report prepared" block.
const ak = [];
ak.push("<!-- answer-key:begin -->");
ak.push("");
ak.push("---");
ak.push("");
ak.push("## Appendix G — Answer Key");
ak.push("");
ak.push("Model answers and worked solutions for every section quick check, *Pause and predict* checkpoint, conceptual question, quiz, problem set, advanced question, challenge problem and Connections problem in this paper. In the HTML edition each answer also sits, collapsed, beneath its question. In the PDF edition the questions are printed without answers, so this appendix is the only place they appear.");
ak.push("");
ak.push('<details class="answer-key" id="answer-key"><summary>Show the full answer key</summary>');
for (const u of keyUnits) {
  ak.push(`<div class="answer-key-unit" id="ak-${u.key}">`);
  ak.push(`<h4>${esc(u.title)}</h4>`);
  u.qcs.forEach((s) => s.qs.forEach((c) => { ak.push(`<p class="ak-q">${s.label}, ${c.q}</p>`); ak.push(c.a); }));
  u.cpKey.forEach((c, i) => { ak.push(`<p class="ak-q">Pause and predict ${i + 1}. ${c.q}</p>`); ak.push(c.a); });
  u.cqs.forEach((c) => { ak.push(`<p class="ak-q">Conceptual ${c.q}</p>`); ak.push(c.a); });
  u.mcqs.forEach((m) => { ak.push(`<p class="ak-q">Quiz ${m.q}</p>`); ak.push(m.expl); });
  u.probs.forEach((p) => { ak.push(`<p class="ak-q">${p.q}</p>`); ak.push(p.a); });
  u.advCqs.forEach((c) => { ak.push(`<p class="ak-q">Advanced ${c.q}</p>`); ak.push(c.a); });
  u.advProbs.forEach((p) => { ak.push(`<p class="ak-q">${p.q}</p>`); ak.push(p.a); });
  u.xProbs.forEach((p) => { ak.push(`<p class="ak-q">${p.q}</p>`); ak.push(p.a); });
  ak.push("</div>");
}
ak.push("</details>");
ak.push("<!-- answer-key:end -->");
const akText = ak.join("\n");
if (/<details class="answer-key"[\s\S]*\n\s*\n[\s\S]*<\/details>/.test(akText)) errors.push("blank line inside the answer key block");
let rIdx = lines.findIndex((l) => l.startsWith("**Report prepared 2026-04-16.**"));
if (rIdx < 0) errors.push("closing 'Report prepared' line not found");
else {
  const i = insertionPoint(rIdx);
  lines.splice(i, 0, "", ...akText.split("\n"), "");
}

let out = lines.join("\n").replace(/\n{3,}/g, "\n\n");
if (errors.length) {
  console.error("SPLICE ERRORS:\n  " + errors.join("\n  "));
  process.exit(1);
}
fs.writeFileSync(paperPath, out);
const counts = keyUnits.reduce((a, u) => {
  a.cp += u.cpKey.length; a.cq += u.cqs.length; a.mcq += u.mcqs.length; a.p += u.probs.length;
  a.acq += u.advCqs.length; a.ap += u.advProbs.length; a.xp += u.xProbs.length;
  a.qcb += u.qcs.length; a.qcq += u.qcs.reduce((n, s) => n + s.qs.length, 0); return a;
}, { cp: 0, cq: 0, mcq: 0, p: 0, acq: 0, ap: 0, xp: 0, qcb: 0, qcq: 0 });
console.log(`spliced ${keyUnits.length} units: ${counts.qcb} quick checks (${counts.qcq} questions), ${counts.cp} checkpoints, ${counts.cq} conceptual, ${counts.mcq} quiz, ${counts.p} problems, ${counts.acq} advanced conceptual, ${counts.ap} challenge problems, ${counts.xp} connections`);
