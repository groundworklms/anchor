# From lookup tool to learning tool — 10 directions

## The problem, stated plainly

What we have is a **doctrine search engine with a good conscience**. It is genuinely
excellent at that. But nobody *learns* from a search box, and the interface offers no
reason to come back tomorrow.

The pedagogy is not ambiguous here. **Retrieval practice** — being made to recall
something — produces far better retention than re-reading. A tutor that only answers
questions is re-reading with extra steps: the student reads a fluent paragraph, feels
informed, and retains almost nothing. Feeling of fluency is not learning, and a
well-written answer actively *inflates* that feeling.

So the single highest-leverage change is a reversal:

> **The tutor should mostly be asking, not answering.**

Everything below is downstream of that.

---

## On the SIGINT course idea — the honest answer

It is the right instinct pointed at the wrong subject. Two hard problems:

1. **We have no SIGINT corpus.** Ours is MCDP 1/1-0/1-1/1-2/1-3/2/3/5/6/7, MCWP 3-11.3,
   TC 3-22.9, TCCC. Building a SIGINT course means sourcing SIGINT doctrine.
2. **Releasability.** Meaningful SIGINT material runs CUI or higher. That directly
   contradicts the public-domain guardrail and the "hand the index to anyone" story that
   makes the air-gapped story work. It is the one subject area most likely to turn a
   clean deployment into a security conversation.

**Use MCDP 1 *Warfighting* instead.** It is the foundational PME text every Marine reads,
taught directly across PME, it is Distribution A, and we already have it chunked to
paragraph level with verified citations. A "Warfighting" course is defensible, on-corpus,
and lands squarely in core PME subject matter.

---

## The ten

### 1. Socratic mode — the tutor asks, the student answers
Flip the interaction. The tutor poses a doctrine question, the student answers in free
text, and the tutor grades the answer *against the retrieved paragraph*, showing what was
missed and citing the page. This is retrieval practice, and it is the single change that
converts this from reference to instruction.
**Why it wins:** it is the same pipeline we already have, run backwards. Retrieval finds
the paragraph, the model generates a question instead of an answer, and grading is
"does the student's answer match the source" — which is exactly the grounding check we
already built.

### 2. A guided track through MCDP 1
Chapters 1–4 as an actual path: *The Nature of War → The Theory of War → Preparing for
War → The Conduct of War*. Each section has a short reading, 3–5 retrieval questions, and
a checkpoint. Progress is visible. There is a next thing to do.
**Why it wins:** structure is the thing a chatbot cannot give. It also maps 1:1 onto the
document structure we already parsed.

### 3. Confidence calibration — the idea that mirrors the product
Before revealing the answer, ask the student: *"how confident are you?"* Then plot their
calibration curve — confidence vs. correctness — over time.
**Why this is the best idea in the list:** the product's entire thesis is *a system that
knows what it does not know*. This teaches the **student** the same skill, and measures
it. A Marine who is confidently wrong about commander's intent is the human version of
the failure mode we spent the whole project engineering against. That symmetry is the
heart of the product, and no lookup tool has it.

### 4. "Find the citation" — teach doctrine navigation
Give the student a claim and make them locate the paragraph that supports it, in the
actual publication. Score on whether they land on the right page.
**Why it wins:** it trains the *findability* skill directly, and it is the student-side
mirror of the findability gap we already measure. A Marine who cannot find the answer in
MCDP 1 has the same problem our retrieval had.

### 5. Spaced repetition over doctrine concepts
Items the student got wrong resurface on a schedule; items they nailed recede. Store the
schedule in the same records database.
**Why it wins:** it is the mechanism that actually produces durable retention, and it
creates the reason to come back tomorrow — the missing incentive.

### 6. Assessment generator for instructors
Generate exam items from a chapter, each with stem, answer, distractors, and the citation
that proves it. Instructor reviews, accepts, rejects, exports.
**Why it wins:** hits *performance assessment* and *content generation*, two of their
seven stated focus areas, and it is instructor-facing — the person who decides whether
this gets adopted.

### 7. Tactical decision vignette, adjudicated against doctrine
A short scenario, a decision point, three courses of action. The student picks and
justifies; the tutor evaluates the justification against doctrine and cites what applies.
**Why it wins:** it is how Marines actually train (TDGs are a real Marine Corps training
tradition) and it is the most *demonstrable* item on this list.

### 8. Mastery map over the corpus structure
A visual of the doctrine — chapters and sections — shaded by demonstrated mastery. Grey
means untouched, amber shaky, green solid.
**Why it wins:** it answers "what do I not know yet?" at a glance, which is the question
a learner actually has, and it makes progress legible in one screen.

### 9. Reading companion mode
The student reads a section in the app; the tutor interjects with a recall question every
few paragraphs, then returns them to the text.
**Why it wins:** it attaches practice to the reading Marines are already assigned, rather
than asking for a new habit.

### 10. Cohort view for the instructor
Aggregate the class: which sections the cohort is weakest on, which questions everyone
misses, which doctrine is unfindable in practice.
**Why it wins:** it is the natural extension of the gap report, and it is the feature that
turns a personal tool into something an institution buys.

---

## What I would actually build, and why

**Three, tightly integrated: #1 Socratic mode, #3 calibration, #2 the MCDP 1 track.**

That combination produces a coherent product rather than a feature list:

- The **track** gives structure and a reason to return.
- **Socratic mode** makes it teach instead of tell.
- **Calibration** gives it a thesis, and the thesis is the same one the device embodies.

The one-line summary writes itself:

> "The device knows what it doesn't know — and it teaches you to know what *you* don't
> know. Here's a Marine's calibration curve after twenty questions on MCDP 1. He was
> ninety percent confident on commander's intent and right forty percent of the time.
> That gap is the most useful thing on this screen, and no schoolhouse currently measures
> it."

#6 (assessment generator) is a natural follow-on: instructor-facing, a different
audience.

## What the interface has to become

Not a prompt box. A prompt box is a blank stare — it puts the burden of knowing what to
ask on the person who by definition does not yet know.

- **Home is a track, not a search bar.** "Continue: Ch 1 · Friction — 3 of 5."
- **One thing on screen at a time.** A question, an answer field, feedback. Not a
  dashboard.
- **Telemetry and trust metrics move off the learner's screen entirely** — they belong in
  the instructor console and the operator-facing view, not in front of a student trying
  to think.
- **Free-form ask stays**, but as a secondary affordance, not the front door.
