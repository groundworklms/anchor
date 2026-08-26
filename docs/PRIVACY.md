# Privacy design — learning records

*Written 25 Aug 2026. This is the design an ATO reviewer or a privacy officer would ask
for: what the prototype does with a Marine's learning record today, why that is acceptable
for a single-user demo appliance and unacceptable the moment the record carries a real
identity, and exactly what a fielded version has to build to close the gap. It describes a
design decision (D-050) and a design target. It is **not** an implementation — no API
code changes on the strength of this document, and none should until fielding is funded.*

Sources this is consistent with, and does not contradict: `docs/FIELDING.md` §3
("Privacy"), `docs/SECURITY_AUDIT.md` findings **H-1** and **M-3**, and decisions
**D-040** (validation, no auth) and **D-050** (the policy). Where those disagree with this
page, they win and this page is wrong.

---

## 1. What the record is, and why privacy is the first question a reviewer asks

The product's pedagogical value *is* the privacy problem. To tell a Marine where they are
confidently wrong, the box keeps a per-item history: every practice item attempted, the
self-rated confidence (1–4), the grade, the free-text answer they typed from memory (up to
2,000 chars), and the calibration that falls out of it — where a Marine believed they were
sure and were wrong. That is a record of an individual's demonstrated weaknesses. It is the
thing that makes the tutor useful and the thing a privacy officer will ask about first.

Today that record is keyed to a client-supplied `learner` string that defaults to the
literal `"default"` (D-040). It is validated and case-folded — 64 chars of `[A-Za-z0-9._-]`,
so `Smith` and `smith` cannot fork into two records — but it is **validated, not
authenticated**: anyone who can reach `:8000` can read anyone's record by naming it. The
moment that string becomes an EDIPI, the appliance becomes an unencrypted, unauthenticated
store of identified training-performance data — a Privacy Act question before it is an
engineering one (SECURITY_AUDIT.md H-1).

---

## 2. The decided policy (D-050): individual to the Marine, aggregate to the instructor

This was decided as policy, not code, and recorded before the data ever carries a real
identifier — so the constraint is designed in rather than discovered after. The decision:

- **The individual owns their record.** A Marine's per-item history and calibration are
  visible to that Marine. **Nobody sees another individual's misses without their
  knowledge.**
- **The instructor sees the aggregate, and only the aggregate.** Class-level mastery and
  the curriculum gap report are the instructor's view. Individual drill-down is not — with
  the single exception of a Marine's own record.
- **Retention is bounded and the Marine can clear their own record.**
- **The identifier stays out of the URL and the logs.**

The decision is *decidable now* because the pedagogically useful thing (knowing where the
**class** is weak) and the privacy-sensitive thing (knowing which **named Marine** is weak)
are separable, and the aggregate carries the teaching value without the individual
exposure.

### 2.1 The instructor boundary is structural, not a UI promise

This is the part that matters to a reviewer, because "the UI doesn't show it" is not a
control. The line is enforced in the shape of the API, not in what the front end chooses to
render:

- `GET /api/instructor/overview` **takes no `learner` parameter.** There is no argument on
  the endpoint that can be pointed at a named Marine.
- The function behind it (`LearnStore.class_overview()`) returns rows keyed by **section**
  plus a single class-size **count**, and never a learner identifier. There are no
  per-learner rows in the response, so a client cannot request one by editing the URL.

An individual's history and calibration live behind separate endpoints
(`/api/learn/history`, `/api/learn/calibration`), each keyed to a learner the caller must
name. On the shared demo appliance that name is the Marine's own convenience identifier; at
fielding it becomes a session-bound identity (§3). The separation is what lets the same box
serve the instructor's legitimate need without handing them a roster of individual
weaknesses.

---

## 3. What a fielded version needs — before the data ever carries a real identifier

None of this is built. It is the checklist that converts "benign demo" into "fieldable",
in the order value-per-unit-of-work runs (consistent with SECURITY_AUDIT.md H-1/M-3 and
D-040). The ordering is deliberate: **items 1–3 must land before the first EDIPI is ever
typed**, because after that the exposure already exists.

1. **Identity is a session, not a URL parameter.** Bind the learner to a server-side
   session established at the start of a study block — a kiosk PIN is enough; it need not be
   a password to forget. This removes the client's ability to *choose* whose record it
   reads (the current "name anyone, read anyone" gap, D-040) and takes the identifier out of
   the query string. Until this exists, everything downstream is keyed to a string the
   client picks.

2. **The identifier never appears in a log.** `learner` currently rides in `GET` query
   strings, which is exactly where identifiers leak into access logs, proxy logs, browser
   history and screenshots (SECURITY_AUDIT.md M-3). Today the box is saved by one thing:
   uvicorn runs at `log_level="warning"`, which suppresses the access log — a privacy
   control that is **one config word from being untrue**. A fielded version does not rely on
   a log level: identity is out of the URL (item 1), and the setting is documented as a
   privacy control, not a verbosity preference.

3. **Store an opaque identifier, not the EDIPI.** Calibration, scheduling and the gap
   report all work on an opaque key — nothing in the learning code needs the plaintext
   EDIPI. Store a per-device HMAC of the identifier (the key already at `record_hmac.key`),
   so a stolen database is not a roster of names. This also protects the signed export
   bundle, which is designed to be handed to an LMS and would otherwise carry
   `{EDIPI, question, answer, timestamp, device_id}` the moment the actor is wired through —
   today a one-line change away (SECURITY_AUDIT.md H-1 §4).

4. **Records are encrypted at rest.** The store is plain SQLite in WAL mode. A pulled SD
   card, a stolen appliance, or a copied `archive/records-*.sqlite` (which `reset_demo.sh`
   deliberately accumulates rather than deletes) reads every Marine's wrong answers in
   plaintext. On a device that travels, full-disk encryption (LUKS on the partition holding
   `/opt/tutor`, or SQLCipher on the database) is the honest answer.

5. **Bounded retention with self-service erase.** There is no delete path, no TTL, and no
   age-based cleanup anywhere today; records live until the disk is wiped, and the archive
   directory grows without bound. A fielded version needs an explicit retention period
   (`PURGE_AFTER_DAYS` plus a startup sweep, extended to `/opt/tutor/archive/`) and a
   documented erasure path so a Marine can clear their own record — the concrete expression
   of "the individual owns their record" from D-050.

6. **Small-N suppression (k-anonymity) on the aggregate.** The instructor boundary (§2.1)
   assumes "the class aggregate" hides the individual. It does not when the cell is small:
   a per-section mastery figure computed over a section that only one Marine has attempted
   *is* that Marine's record wearing an aggregate's clothes, and the class-size count makes
   the small cell visible. A fielded `class_overview()` must **suppress any section cell
   below a threshold k** (report "insufficient data", not a number), so a lone Marine cannot
   be re-identified through the aggregate the policy said the instructor may see. This is
   the difference between the aggregate being structurally safe and merely looking
   aggregated.

---

## 4. What is deliberately NOT built for the prototype, and why that is defensible

Stated plainly, because pretending the prototype is fielding-ready is the fastest way to
lose a reviewer's trust (the posture `docs/FIELDING.md` takes throughout):

- **No authentication.** Anyone reaching `:8000` can read any record by naming it.
- **No encryption at rest.** Plain SQLite.
- **No retention limit and no erase path.** Records and archives accumulate.
- **The identifier travels in the URL.**

All four are real, and all four are **acceptable for the prototype for exactly one reason:
`"default"` is not a person.** The single-user demo appliance holds one anonymous record;
the signed export confirms it (`actor = "anonymous-device-user"`, no personal data). There
is no identified subject to protect, so there is nothing to encrypt, authenticate, retain-
limit or hide. The controls in §3 are absent by *scope*, not by oversight.

What makes this defensible rather than negligent is that the gap is **named, bounded, and
gated**:

- It is **named** — this document and SECURITY_AUDIT.md H-1/M-3 describe it precisely.
- It is **bounded** — the trigger is a single, visible event: the first time `learner`
  carries a real identifier. The identifier is already plumbed end to end, so that event is
  a small code change, which is exactly why §3 items 1–3 are marked "before the first EDIPI
  is typed" and not "eventually".
- It is **gated** — fielding is gated on an ATO under the Risk Management Framework
  (DoDI 8510.01) and, for identified training data, plausibly a Privacy Impact Assessment.
  The policy in §2 is settled so it can be *designed in* at that point rather than
  retrofitted after records exist. This page is the artefact that gate will ask for.

The one-line summary for a Marine — the version that has to be true, not just compliant —
is the one D-050 and FIELDING.md already commit to: **your instructor sees where the class
is weak; nobody sees your individual misses without your knowledge.** Everything in §3
exists to make that sentence structurally true rather than a promise.
