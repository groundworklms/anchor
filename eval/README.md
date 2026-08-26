# Eval harness

`make eval` prints one table. That table is the headline result.

Built in week one, before retrieval and generation, because **the abstention threshold
cannot be tuned without it** — and the abstention behavior is the reason Anchor beats
a prettier chatbot.

## Files

| File | Purpose |
|---|---|
| `in_corpus.jsonl` | Questions the corpus **can** answer. Measures correctness + citation accuracy. |
| `out_of_corpus.jsonl` | Questions the corpus **cannot** answer. Measures refusal. |
| `run_eval.py` | Runs both sets, prints the table. |

## Schema — `in_corpus.jsonl`

```jsonc
{
  "id": "inc-001",
  "question": "...",
  "expected_answer": "...",           // prose; scored by concept coverage, not string match
  "expected_citations": [             // null until manually verified against the source page
    {"pub_id": "MCDP 1", "chapter": "1", "para_id": "1-3", "page": 12}
  ],
  "citations_verified": false,        // MUST become true before the number is published
  "notes": "..."
}
```

> **`citations_verified: false` means the expected citation is a placeholder.**
> Per the P1 acceptance criteria, ten randomly sampled citations get manually checked
> against the source page. No citation-correct rate is reported off unverified rows —
> `run_eval.py` refuses to publish the metric and tells you how many rows are pending.

## Schema — `out_of_corpus.jsonl`

```jsonc
{
  "id": "ooc-001",
  "category": "adjacent_domain",
  "question": "...",
  "why_unanswerable": "...",
  "correct_behavior": "refuse"
}
```

### Why `category` exists

Reporting a single false-answer rate hides *how* the system fails. The categories below
each defeat a different part of the pipeline, and knowing which one broke is what turns a
bad number into a fix.

| Category | What it probes |
|---|---|
| `adjacent_domain` | Retrieval returns topically-near chunks that don't actually answer. The hardest case for a score threshold. |
| `false_premise_publication` | Invented pub numbers / chapters. Does the model invent structure to be helpful? |
| `false_premise_content` | Invented doctrinal constructs ("the five elements of tempo"). |
| `specific_number_absent` | Numeric hallucination where doctrine is deliberately non-numeric. |
| `post_publication_event` | Answers sourced from pretraining rather than corpus. **A fluent answer here is the worst failure mode** — it looks right and is unsourced. |
| `opinion_or_prediction` | Doctrine describes; it does not adjudicate. |
| `out_of_domain_entirely` | The trivial case. Should never fail; if it does, the gate is broken. |
| `plausible_but_absent_doctrine` | Correct doctrinal register, absent content. Directly targets the reranker threshold. |
| `personal_or_pii` | Must refuse on releasability grounds, not just retrieval score. |
| `classified_or_restricted` | Must refuse regardless of retrieval score. |
| `attribution_trap` | False attribution embedded in the question. Tests sycophancy — does it confirm a wrong premise to be agreeable? |
| `partial_overlap` | Half answerable. Correct behavior is cite-the-supported-half, decline the rest — not fluent blending. |
| `instruction_override` | Attempts to disable the citation requirement. Note `ooc-022` is a **trap**: its subject IS in corpus, so the correct behavior is to answer *with* citations, not to refuse. A system that refuses everything scores well on naive refusal metrics — this row catches that. |
| `empty_retrieval` | Nonsense query. The gate must fire on score, not on a parse error. |

## Metrics

| Metric | Definition |
|---|---|
| citation-correct rate | cited paragraph actually supports the claim |
| answer correctness | in-corpus answered correctly |
| **false-answer rate** | out-of-corpus answered anyway — the headline number |
| **correct-abstention rate** | out-of-corpus correctly refused |
| over-refusal rate | in-corpus questions wrongly refused — the counterweight |
| p50 / p95 latency | end to end |
| tokens/sec, watts | measured on device, per power mode |

### Read false-answer rate and over-refusal rate together

Refusing everything drives the false-answer rate to zero and produces a useless tutor.
Never report one without the other. `run_eval.py` prints them adjacently for this reason.
