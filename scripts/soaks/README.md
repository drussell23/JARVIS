# Battle-test soak launchers

`soakN.sh` is the exact environment one farming soak was launched with,
kept so a result can be reproduced or attributed after the fact. They were
untracked for months, which meant the only record of *what an env actually
was* lived in a shell history — and two soaks were re-run under an env
nobody could reconstruct.

Each is a thin wrapper: export the variables under test, then `exec`
`scripts/ouroboros_battle_test.py`. The header comment states the question
the run existed to answer, the proof lines to grep for, and the baseline
it must beat. **Read the header before re-running one.**

## These carry machine paths

Every launcher begins `cd /mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis` and
execs `/home/jarvis_svc/.venvs/ov/bin/python`. That is deliberate — they
are a record of what ran on this host, not a portable harness. On another
machine, change those two lines; nothing else is host-specific.

## The arc, in order

Soaks 1-4 predate the trajectory recorder and left no corpus.

| soak | question | outcome |
|---|---|---|
| 5-12 | can a farming soak reach GENERATE at all on the local lane? | starvation sources found one at a time — DW topology, budget caps, work-order supply |
| 13-17 | does sibling entropy produce *distinct* answers? | sampling plumbing, reward band geometry, native `/api/chat` transport |
| 18 | is the corpus clean once lineage is purified? | 59 rows, best spread 0.0059 → **0.0131** with the context-aware reward; 1 trainable group |
| 19 | does BREADTH (9 → 41 authored tasks) clear the gate? | **Gate 3 PASSED.** 85 rows / 29 prompts / **4 trainable groups** (2 with reward off) |
| 20 | does re-running the same batch deepen those groups? | **NULL RUN.** `WorkOrderSensor` emitted 0 orders — the cross-session seen-ledger suppressed all 41. 22 rows, none trainable |
| 21 | control: same env, with the ledger bypass | ran clean; **13/28 singleton ops (46%)** — the pathology reproduced exactly. Cumulative: 91 rows / 50 prompts / 6 groups |
| 22 | does sibling fulfillment cut the singleton rate? | first ledgers showed `noop>merged` saving slots the old loop forfeited |
| 23 | do refusals-as-rows expand the yield? | escalation multiplier reverted to 1.0; refusals now persist as gradable rows |

## Two traps these encode

**The seen-ledger.** `.jarvis/work_order_seen.json` is a *cross-session*
dedup ledger, so re-running an unchanged `progress.md` emits nothing. That
is what made soak 20 worthless. Set `JARVIS_ALLOW_ROADMAP_REVISIT=true`
(soak 21 onward) rather than deleting the file — deleting it destroys the
operator's record to buy one run. Grep the session log for
`emitted .* work order` within two minutes of launch: absent means the run
is already worthless and should be killed, not left to burn 2.5 h.

**One variable at a time.** From soak 21 on, each launcher is the previous
one byte-for-byte with a single documented change, and says so in its
header. Soaks 22 and 23 additionally refuse to start unless `HEAD` carries
the commit they exist to test — a soak that silently runs without the fix
produces a confident wrong answer.

## What a soak cannot do

Groups are keyed on the exact prompt text and every prompt embeds its own
Op-ID, so a new soak always mints new groups and can never add rows to an
existing one. Re-running buys **breadth**, never depth. Depth comes from
siblings of a single op — `JARVIS_LOCAL_SIBLING_CANDIDATES` and whether
the sibling loop actually fulfils that quota.
