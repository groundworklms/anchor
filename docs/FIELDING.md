# Fielding concept

*Written 24 Aug 2026. This is the honest version — what it would actually take to put
this in front of Marines, including the parts that are not engineering problems.*

The prototype is one Jetson Orin Nano holding thirteen publications and running entirely
offline. That is a working demonstration, not a fielding plan. The gap between the two is
mostly policy, and pretending otherwise is the fastest way to lose credibility with the
people who would have to sign for it.

---

## 1. The board is the proof, not the product

The instinct is to price the demo hardware against the force. Don't:

| | |
|---|---|
| Orin Nano dev kit, retail | $249 (verified) |
| Active-duty Marines | ~170,000 |
| Total Marine Corps incl. Reserve and civilians | ~215,000 |
| Naive cost of one board each | **~$42M–$54M** |

No one signs that, and they shouldn't — it buys 200,000 single-purpose appliances that
need physical sustainment. The board earns its place for exactly one reason: it makes
"zero network calls at inference time" a claim you can test by unplugging a cable, in
front of a skeptic, in ten seconds. That is worth a great deal at a demo table and very
little in a supply chain.

What actually travels is the software: FastAPI, SQLite, and llama.cpp, with a 15.8 MB
index and a quantised model. Nothing in the inference path is Jetson-specific except the
CUDA build of llama.cpp, which has CPU and other-GPU equivalents.

## 2. Three distribution paths, in the order I would pursue them

### A. One box per schoolhouse, company office, or deployed COC — *start here*

Order of thousands of units, not hundreds of thousands. It lives in the company office,
the barracks dayroom, or a COC on a ship with no reachable network.

- **Why it is the right first step:** it is the only path where the offline property is
  the *point* rather than a limitation, and it is small enough to fund out of a unit
  budget rather than a program of record.
- **Cost:** hardware is the small number. The real cost is whoever keeps the corpus
  current and reviews generated items.
- **Blocked on:** a distributor quote at quantity (only $249 retail is verified), and
  whether a non-networked appliance in a government space needs an ATO at all — see §3.

### B. The same software on hardware Marines already have

A laptop or desktop runs this stack. The offline guarantee survives; the "unplug the
cable" theatre does not.

- **Why:** reaches far more people for near-zero marginal hardware cost.
- **Blocked on:** software approval to install on a government machine, which is a
  heavier lift than a standalone box — and the model weights are several GB per install.

### C. Hosted on MarineNet or equivalent

Reaches everyone. Also discards the property the whole project is built around.

- **Only worth doing** if the abstention behaviour and the human review gate travel with
  it. A hosted version that quietly adds a cloud fallback is a different product wearing
  this one's name.

## 3. What actually gates fielding — none of it is code

**Authority to Operate.** DoD IT requires an ATO under the Risk Management Framework
(DoDI 8510.01), granted by an Authorizing Official. The prototype has none, and no AO has
been approached. An air-gapped standalone appliance may fall under a lighter path than a
networked system — that is a question for an AO and a Information System Security
Manager, not something to assume in a slide. **Naming a specific AO is the highest-value
next action in this document.**

**Section 508.** Federal ICT must be accessible. Audited 24 Aug against WCAG 2.1 AA
(`docs/ACCESSIBILITY.md`) and **it does not conform**: 10 criteria fail, 6 at level A and
4 at AA, against 19 that pass. The failures are live regions, accessible names on custom
controls, keyboard operability of the publication cards, and reflow below ~460px. The
dark palette works in its favour — body text measures 17.94:1 — and the audit is
specific enough to fix, but conformance is not a claim that can be made today.

**Privacy.** Learning records today are keyed to a `learner` string that defaults to
`"default"`. The moment that becomes an EDIPI, this holds records of individual Marines'
demonstrated weaknesses — which is exactly what makes it pedagogically useful and exactly
what makes it a privacy question. Needs a decision on retention, who can see a
subordinate's calibration data, and whether a Privacy Impact Assessment applies. **The
answer that a Marine will accept is that their instructor sees the aggregate and nobody
sees their individual misses without their knowledge.** That is a design constraint, and
it should be decided before the data exists rather than after.

**Doctrine currency.** MCDPs get revised. Nothing in the current system notices. Someone
has to own: watching MCPEL for new editions, re-running ingest, re-reviewing generated
items whose source paragraph changed, and getting the update onto every fielded box —
which, for an air-gapped appliance, means physical media or a person with a USB stick.
An out-of-date doctrine tutor that still sounds confident is worse than no tutor.

**Content review at scale.** Every practice question is model-written and human-approved.
That gate is the reason this is defensible; it is also the recurring cost. Measured on
the full 1,011-item bank: **88.5% cleared the automatic checks and 11.5% needed a human to
read them properly.** Of those 116, 36 were approved as written, 73 were edited, and 7
were rejected outright — so a third of the flags were false alarms and the checker earns
its keep by directing attention, not by deciding. That is roughly a person-day of
subject-matter-expert time per corpus refresh: real, affordable, and not zero.

## 4. What I would say to a sponsor

The defensible ask is not "buy 200,000 boards." It is:

1. Fund a **pilot at one schoolhouse** — twenty boxes, one corpus, one instructor who
   owns review.
2. Measure the thing nobody measures: whether Marines who train against calibration
   feedback are less confidently wrong six weeks later than Marines who do not. That is a
   real experiment, it is cheap, and no vendor slide has the answer.
3. Decide on the strength of that, not on the strength of a demo.

## 5. Known-unfinished, stated plainly

- Cold boot straddles the 60s budget (52.9–60.7s; variance is in UEFI firmware).
- Battery runtime has never been measured.
- A block-level clone of the boot drive still has to be taken with the device idle.
- The kiosk has never run against a physical display.
- Over-refusal is 15.9%. In 11 of 12 cases the correct paragraph was retrieved and then
  scored below the gate, so this is reranker calibration rather than corpus coverage.
- 10 WCAG 2.1 AA criteria fail; the fixes are identified but not applied.
- Single-learner. No accounts, no unit rollup, no instructor view of a squad.
