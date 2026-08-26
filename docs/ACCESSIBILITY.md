# Accessibility Audit — Doctrine Tutor UI

**Target:** `ui/index.html` (single file, 971 lines)
**Standard:** Section 508 of the Rehabilitation Act → **WCAG 2.1 Level AA**
**Date:** 2026-08-24
**Audit mode:** **LIVE.** Playwright/Chromium driven against the running instance at
`http://127.0.0.1:8001/` (1440×900), plus static reading of the source. All non-GET
requests were aborted at the network layer for the whole session — **no POST reached the
device**, no answer was submitted, no review decision was recorded.

---

## Verdict

**No. This interface is not Section 508 conformant today.** Ten WCAG 2.1 success criteria
fail — six at Level A, four at Level AA — and the Level A failures are the serious ones:
one control (`.pubcard`) cannot be operated by keyboard at all, four of the eight tab stops
on the home screen land on invisible offscreen elements inside the closed admin drawer, the
confidence selector communicates its selected state to sighted users only, and the
instructor review screen's single-key shortcuts fire on `Ctrl+A` / `Cmd+A` / `Ctrl+R`
— meaning a reviewer pressing Select All irreversibly approves a doctrine question for
issue to Marines. That last one is a correctness bug as much as an accessibility one. The
good news is that the foundations are genuinely sound: the type scale is large, the palette
is mostly high-contrast, the view-switching correctly removes hidden screens from the
accessibility tree, text survives the WCAG 1.4.12 spacing overrides intact, and the
suspected `--text-3` contrast problem **does not exist**. Most of what fails is shallow —
roughly two-thirds of the findings are attributes and hex values, not rewrites.

---

## What was checked, and what could not be

Live data on the device at audit time: **1 publication** (MCDP 1, 74 items), **review queue
empty** (74/74 approved), **8 answers recorded** (calibration has real data).

Because only one publication is loaded, `renderPubs()` returns early and the `.pubcard`
branch of `loadTrack()` never runs; because the review queue is empty, `renderReview()`
replaces `#rvbody` wholesale and the flag badges never render. Those three controls were
therefore audited by calling the application's **own render functions** in the browser with
synthetic data (no network involved), so the markup measured is exactly the markup the app
produces. This is noted per-finding below.

---

## Findings

| # | WCAG SC | Lvl | What fails | Element / selector | Measured evidence |
|---|---------|-----|-----------|--------------------|-------------------|
| 1 | **2.1.4** Character Key Shortcuts | A | Single-key shortcuts `A`/`R`/`E`/`S` are active document-wide whenever the review view is visible, cannot be turned off or remapped, and **no modifier is checked** | `index.html:949` keydown listener | Instrumented with `decide()` stubbed: `Ctrl+A` → `decide:approved`; `Cmd+A` → `decide:approved`; `Ctrl+R` → `decide:rejected`; `Ctrl+S` → skip; `Alt+A` → `decide:approved`; plain `a` while `#rvskip` has focus → `decide:approved` |
| 2 | **2.1.1** Keyboard | A | Publication cards are `<div>` with an `onclick` — not focusable, not operable by keyboard | `.pubcard` (`index.html:222`, built in `loadTrack()`) | `tagName=DIV`, `tabIndex=-1`, `role=null`, `isFocusable=false`. Absent from the tab order: rendering 3 pubcards + 4 pubtabs produced 12 tab stops, **none of them a pubcard** |
| 3 | **2.4.3** Focus Order | A | The admin drawer is offscreen (`translateX(100%)`) but still in the tab order when closed; focus is not moved into it on open, is not trapped while open, and is left stranded inside it after `Escape` | `#drawer`, `#scrim` | Home view, drawer closed: **4 of 8 tab stops are offscreen** — `#closeadmin` (x=1910), `#openreview` (x=1463), `#adminq` (x=1463), `#adminask` (x=1916) on a 1440px viewport. Drawer open: tab order still visits `#opencal`→`#openadmin`→`#startbtn`→`#openhist` behind the scrim; `main` has `inert=false`, `aria-hidden=null`. After `Escape`: `document.activeElement` = `BUTTON#closeadmin`, still inside the now-hidden drawer |
| 4 | **4.1.2** Name, Role, Value | A | Confidence selector is four plain `<button>`s; selected state is conveyed by a CSS class only | `#confbtns button` (`index.html:96–99`) | After clicking "Guessing": `class="on"`, but `aria-checked=null`, `aria-pressed=null`, `role=null`; wrapper `#confbtns` has `role=null`, `aria-label=null`. A screen reader hears four identical buttons and no selection |
| 5 | **4.1.2** Name, Role, Value | A | Publication tabs carry no selected state, and their accessible name is corrupted by the unseparated count span | `.pubtab` (`index.html:239`, `renderPubs()`) | Chromium AX tree: `role=button name='MCDP 18/74'`, `name='MCWP 3-013/60'` — the `<span class="n">8/74</span>` concatenates directly onto the pub id. `aria-selected=null`, `aria-pressed=null`, `aria-current=null` on all four, including the one with `class="pubtab on"` |
| 6 | **4.1.2** Name, Role, Value / **3.3.2** Labels or Instructions | A | The review question edit field has **no accessible name at all** — no label, no `aria-label`, no placeholder | `#rvqedit` (`index.html:343`) | `ariaLabel=null`, `placeholder=null`, `label[for=rvqedit]` does not exist. (Its sibling `#rvptsedit` *does* have `aria-label="Key points, one per line"` — the omission looks accidental) |
| 7 | **3.3.2** Labels or Instructions | A | The answer box and the ask box are labelled by placeholder only, which disappears on first keystroke | `#ans` (`index.html:305`), `#adminq` (`index.html:451`) | Both: `ariaLabel=null`, `labelFor=false`, name derived solely from `placeholder` |
| 8 | **1.3.1** Info and Relationships | A | The visible group label for the confidence buttons is not programmatically associated with them; the submit hint is not associated with the submit button | `.conf .q` (`index.html:307`) → `#confbtns`; `#submithint` → `#submit` | `#confbtns` has no `aria-labelledby`; `#submit` has `aria-describedby=null` while `#submithint` reads "Write an answer and pick a confidence." |
| 9 | **4.1.3** Status Messages | AA | Nothing in the application is a live region. Grading results, the streaming/loading state, the 4-second network poll, and the bulk-clear result are all silent to a screen reader | whole app | `document.querySelectorAll('[aria-live]')` → **`[]`**. `[role=status]`, `[role=alert]`, `[role=log]`, `<output>` → **`[]`**. Verified individually on `#verdictrow`, `#kp`, `#src`, `#feedback`, `#submithint`, `#nettext`, `#adminanswer`, `#rvbulkhint`, `#railcap`, `#homehint`, `#rvcount`, `#admin-nettext`: all `aria-live=null, role=null` |
| 10 | **1.4.3** Contrast (Minimum) | AA | Eight foreground/background pairs fall below 4.5:1 at their rendered size | see contrast table below | Worst: `--critical` on the `.verdict.incorrect` tint = **3.25:1**; `.ok` button `#fff` on `--good` = **3.35:1**; `.primary` `#fff` on `--accent` = **3.64:1** |
| 11 | **1.4.11** Non-text Contrast | AA | `--line` is used as the boundary of active controls (text inputs, ghost buttons, confidence buttons, chips) at ~1.4:1; the confidence buttons' unselected state has no perceptible boundary | `--line` (`index.html:14`) on `textarea`, `.rvedit`, `.askbox input`, `.ghost`, `.confbtns button`, `.chip`, `.duechip` | `--line #34332e` on `--surface-1 #171716` = **1.42:1**; on `--surface-2 #201f1e` = **1.30:1**. Both need 3:1. Confidence-button state change by fill alone (`#201f1e`→`#17263a`) = **1.08:1**. `.danger` border `rgba(208,59,59,.5)` over `--surface-1` = **1.79:1** |
| 12 | **1.4.10** Reflow | AA | The header does not wrap; two buttons overflow the viewport below ~460 CSS px, forcing horizontal scrolling | `header` (`index.html:42`) | At 320×900: `document.scrollWidth=460` vs `clientWidth=320` → **horizontal scroll**. Overflowing: `#opencal` right edge **374px**, `#openadmin` right edge **460px**. Also fails at 400px. Passes at 768px and above. `<main>` itself never overflows — the failure is the header alone |

**Totals: 10 criteria failing — 6 Level A (SC 1.3.1, 2.1.1, 2.1.4, 2.4.3, 3.3.2, 4.1.2), 4 Level AA (SC 1.4.3, 1.4.10, 1.4.11, 4.1.3).**

---

## What passes — with the measurement that proves it

These were tested and are genuinely fine. Several were on the suspect list.

| WCAG SC | Lvl | Result | Evidence |
|---------|-----|--------|----------|
| **1.4.3** for `--text-3` | AA | **PASS — the main suspicion was wrong** | `--text-3 #8b8a80` on `--surface-1 #171716` = **5.16:1**; on `--surface-2` = **4.74:1**; on `--bg` = **5.56:1**. All above 4.5:1 at every size it is used (11px–15px). No `--text-3` pair anywhere in the app falls below 4.5:1 |
| **2.1.1** for the A/R/E/S shortcuts in text fields | A | **PASS — the suspected dictation bug does not exist** | The handler at `index.html:951` returns early on `INPUT`/`TEXTAREA`. Instrumented: `"a"` dispatched at `#rvqedit` → `[]`; `"r"` at `#rvqedit` → `[]`; `"a"` at `#rvptsedit` → `[]`. Typing is safe. (The shortcuts still fail 2.1.4 for the separate reasons in finding 1) |
| **1.1.1** Non-text Content — calibration bars | A | **PASS** | The numeric percentages added beside the bars form a complete text equivalent. A screen reader reads one row as: `"Certain 6 answers said this sure · 100% actually right · 33% -67 point gap"`. The `.calbar` elements are empty `<div>`s that contribute nothing to the AX tree, so they add no noise either |
| **1.4.1** Use of Color — network chip | A | **PASS** | `#netchip` exposes real text, not just the dot: `textContent = "Air-gapped"` with `body[data-net="OFFLINE"]`. The drawer banner likewise carries `#admin-nettext = "AIR-GAPPED"` and a subtitle. Colour is redundant throughout, not the sole channel. Same for the verdict pills, flag badges, and due chips, all of which carry text |
| **4.1.2** hidden views | A | **PASS** | The `display:none` view-switching correctly removes screens from the accessibility tree. With the home view active, `"Submit answer"` and `"How well do you know…"` are **absent** from the full Chromium AX tree; `#view-practice`/`#view-review`/`#view-hist`/`#view-cal` all report `display:none` and their `<h1>`s report `visible:false` |
| **2.4.7** Focus Visible | AA | **PASS** | Buttons keep the Chromium platform ring. Pixel-diffed focused vs unfocused screenshots: the ring paints `rgb(255,255,255)` over `rgb(23,23,22)` = **17.94:1** (808 changed px on `.primary`, 604 on `.ghost`). For the three inputs where `outline:none` is set, the replacement is a 2px border change `--line #34332e` → `--accent #3987e5`, a **3.48:1** state change at **4.52:1** against the field fill — visible, though weaker than the native ring (see fix 8) |
| **1.4.4** Resize Text | AA | **PASS** | At 200% zoom on a 1440×900 monitor (720×450 CSS px): `scrollWidth=720`, `clientWidth=720`, zero clipped elements in the header or card content |
| **1.4.12** Text Spacing | AA | **PASS** | Injected the WCAG override (`line-height:1.5; letter-spacing:0.12em; word-spacing:0.16em; p margin-bottom:2em`): **0 elements clipped**, no horizontal scroll, `scrollWidth` unchanged at 1440. The `nowrap`/`ellipsis` rows (`.sec .name`, `.histrow .q`, `.pcname`) survive |
| **1.3.1** heading hierarchy | A | **PASS** | Outline is `h1` → `h2` throughout with **no skipped levels**. Nine headings total, one visible `h1` per view. (Minor: the drawer's "How it works" title is a `<div class="brand">`, not a heading — see fix 12) |
| **3.1.1** Language of Page | A | **PASS** | `<html lang="en">` |
| **2.4.2** Page Titled | A | **PASS** | `<title>Warfighting — Doctrine Tutor</title>` (advisory: the title never updates as the SPA changes views) |
| **1.3.6 / landmarks** | — | **PASS** | `banner`, `main`, and `complementary` landmarks are all exposed. The five `<section>` views are correctly *not* exposed as regions (unnamed sections stay generic), so they add no landmark clutter |
| **2.3.1** Three Flashes | A | **PASS** | No flashing content. Only two transitions exist: `.rail .fill { width .4s }` and `.drawer { transform .22s }` |
| **2.2.1 / 2.2.2** Timing | A | **PASS** | No time limits. The 4-second network poll changes a short text chip; it is not moving, blinking, or auto-updating content requiring a pause control |
| **2.4.1** Bypass Blocks | A | **PASS / N.A.** | Only four controls precede `<main>`; there is no repeated navigation block to bypass |
| **2.5.3** Label in Name | A | **PASS** | Every accessible name contains the visible label. `"Approve A"`, `"Reject R"`, `"Practise 3 due"`, `"See how it works"` — all match their visible text |
| **3.2.1 / 3.2.2** On Focus / On Input | A | **PASS** | No context changes on focus or input |
| **1.4.13** Content on Hover or Focus | AA | **PASS / N.A.** | No tooltips or hover-revealed content |
| **Accessible names** | — | **PASS (except finding 6)** | The Chromium AX tree found **zero** controls with an empty accessible name in the live home + drawer view. Every rendered button had a name |

**Note on an apparent failure that is not one:** the disabled `.primary` button computes to
**3.23:1** (`#fff` at 45% opacity over `--accent` at 45% over `--surface-1`). WCAG 1.4.3
explicitly exempts text that is part of an **inactive** user interface component, so this
is not a failure. It is still hard to read and worth improving.

---

## Full contrast measurements

51 text pairs and 22 non-text pairs were computed from the actual CSS custom properties,
with `rgba()` overlays composited against their real backdrops. Only the failures and the
notable passes are reproduced here.

### 1.4.3 Contrast (Minimum) — the 8 failures

| Pair | Rendered size | Ratio | Required | Result |
|------|---------------|-------|----------|--------|
| `.verdict.incorrect` — `--critical` on `rgba(208,59,59,.16)` over `--surface-1` (`#351d1c`) | 17px/700 | **3.25:1** | 4.5:1 | FAIL |
| `.flag.UNGROUNDED_KEY_POINT` — `--critical` on same tint | 13px | **3.25:1** | 4.5:1 | FAIL |
| `.ok` button — `#fff` on `--good #0ca30c` | 17px/700 | **3.35:1** | 4.5:1 | FAIL |
| `.primary` button — `#fff` on `--accent #3987e5` | 17px/700 | **3.64:1** | 4.5:1 | FAIL |
| `.danger` button — `--critical` on `--surface-1` | 17px/600 | **3.73:1** | 4.5:1 | FAIL |
| `.histrow .v.incorrect` — `--critical` on `--surface-1` | 13px/700 | **3.73:1** | 4.5:1 | FAIL |
| `.verdict.correct` — `--good` on `rgba(12,163,12,.16)` over `--surface-1` (`#152d14`) | 17px/700 | **4.42:1** | 4.5:1 | FAIL |
| `.state.mastered` — `--good` on same tint | 12px/600 | **4.42:1** | 4.5:1 | FAIL |

Every failure involves `--good` or `--critical` at small-to-medium size, or white text on a
saturated fill. **`--warning` passes everywhere** (worst case 7.06:1). `--accent` as *text*
passes (4.93:1 on `--surface-1`).

### 1.4.3 — representative passes

| Pair | Size | Ratio | Req | Result |
|------|------|-------|-----|--------|
| `--text-1` on `--bg` | 17px | 19.31:1 | 4.5 | PASS |
| `--text-1` on `--surface-1` | 17px | 17.94:1 | 4.5 | PASS |
| `.qtext` `--text-1` on `--surface-1` | 27px/600 | 17.94:1 | 3.0 | PASS |
| `--text-2` on `--surface-1` | 14–16px | 10.01:1 | 4.5 | PASS |
| `--text-2` on `--surface-2` | 15–16px | 9.18:1 | 4.5 | PASS |
| **`--text-3` on `--surface-1`** (h2, `.hint`, `.muted`, `.railcap`, `.caltag`, `.keyhint`) | 11–15px | **5.16:1** | 4.5 | **PASS** |
| **`--text-3` on `--surface-2`** (`.cite`, `.state.new`, `.flag.none`) | 12–13px | **4.74:1** | 4.5 | **PASS** |
| `--warning` on `.verdict.partial` tint | 17px/700 | 7.06:1 | 4.5 | PASS |
| `--good` on `--surface-1` (`.histrow .v.correct`) | 13px/700 | 5.35:1 | 4.5 | PASS |
| `--text-1` on `#17263a` (selected confidence button) | 15px/600 | 15.28:1 | 4.5 | PASS |
| `.kp .hit` / `.kp .miss` — `--text-1` on 10% tints | 16px | 16.04 / 14.95:1 | 4.5 | PASS |
| `.banner-full` `--critical` on `#2e1414` | 20px/700 | 3.56:1 | 3.0 | PASS |
| `.pair .good .n` — `--good` on `--surface-1` | 30px/700 | 5.35:1 | 3.0 | PASS |

### 1.4.11 Non-text Contrast — failures and passes

| Element | Ratio | Result |
|---------|-------|--------|
| `--line` border on `--surface-1` (ghost buttons, confidence buttons, chips) | **1.42:1** | FAIL |
| `--line` 2px border on `--surface-2` (textarea, `.rvedit`, `.askbox input`) | **1.30:1** | FAIL |
| `.danger` border `rgba(208,59,59,.5)` on `--surface-1` | **1.79:1** | FAIL |
| Confidence button state change, fill only (`#201f1e` → `#17263a`) | **1.08:1** | FAIL |
| `--line-soft` row dividers on `--surface-1` | 1.17:1 | Exempt (decorative, not a control boundary) — but invisible in practice |
| `--surface-1` card fill vs `--bg` page | 1.08:1 | Exempt (decorative) — noted only because it means the `--line` border does *all* the work of separating a card |
| `--accent` focus border on `--surface-2` | 4.52:1 | PASS |
| `--accent` selected border, `.confbtns.on` / `.pubtab.on`, on `--surface-1` | 4.93:1 | PASS |
| Progress fill `--accent` on `--surface-2` track | 4.52:1 | PASS |
| `.calbar.stated` `--text-3` on `--surface-2` track | 4.74:1 | PASS |
| `.calbar.actual` `--good` on `--surface-2` track | 4.91:1 | PASS |
| Chip status dots (`--good` / `--warning` / `--critical` / `--text-3`) on `--surface-1` | 5.35 / 9.78 / 3.73 / 5.16:1 | PASS |

---

## Prioritised fixes

### Cheap and high value

**Fix 1 — Guard the review shortcuts against modifier keys and make them opt-out (SC 2.1.4, Level A).**
This is the highest-priority item in the report: `Ctrl+A` currently approves a doctrine
question. In `index.html:949`, inside the keydown listener, add the modifier guard before
the key comparison and restrict the shortcut to when focus is not on a control:

```js
document.addEventListener("keydown", function (e) {
  if ($("#view-review").style.display === "none") return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;          // <-- ADD
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") {
    if (e.key === "Enter" && rv.editing) decide("approved");
    return;
  }
  if (e.target.closest && e.target.closest("button")) return;  // <-- ADD
  if (window.__shortcutsOff) return;                            // <-- ADD (opt-out)
  const k = e.key.toLowerCase();
  ...
});
```

For full 2.1.4 conformance the shortcuts must be turn-off-able or remappable. The
`__shortcutsOff` flag above plus one checkbox in the review view satisfies the "turn off"
provision — that is the cheapest of the three permitted remedies.

**Fix 2 — Give the confidence buttons radiogroup semantics (SC 4.1.2 + 1.3.1, Level A).**
Two small edits. In the HTML at `index.html:306-308`:

```html
<div class="conf">
  <div class="q" id="conflabel">Before you submit — how sure are you?</div>
  <div class="confbtns" id="confbtns" role="radiogroup" aria-labelledby="conflabel"></div>
</div>
```

In `renderConfidence()`, emit the role and initial state, and keep `aria-checked` in sync
with the `.on` class:

```js
$("#confbtns").innerHTML = Object.entries(labels)
  .map(([k, v]) => `<button role="radio" aria-checked="false" data-c="${k}">${v}</button>`).join("");
document.querySelectorAll("#confbtns button").forEach(b => b.onclick = () => {
  state.confidence = +b.dataset.c;
  document.querySelectorAll("#confbtns button").forEach(x => {
    const on = x === b;
    x.classList.toggle("on", on);
    x.setAttribute("aria-checked", on ? "true" : "false");   // <-- ADD
  });
  updateSubmit();
});
```

Also reset `aria-checked="false"` alongside the `classList.remove("on")` loop in
`nextQuestion()`.

**Fix 3 — Add live regions (SC 4.1.3, Level AA).** Six attributes close this criterion
entirely. Grading results and the ask answer are user-initiated and substantive → `polite`.
The network chip is a background poll → `polite` (never `assertive`; it fires every 4
seconds and would make the app unusable).

```html
<span class="chip" id="netchip" role="status" aria-live="polite" aria-atomic="true">
  <span class="dot" aria-hidden="true"></span><span id="nettext">checking…</span></span>
...
<span class="muted" id="submithint" role="status" aria-live="polite"></span>
...
<div id="feedback" style="display:none" aria-live="polite" aria-atomic="false">
...
<div id="adminanswer" class="hint" role="status" aria-live="polite"></div>
...
<div class="hint" id="rvbulkhint" role="status" aria-live="polite">
```

Put the region on `#feedback` (the stable wrapper) rather than on `#verdictrow`/`#kp`/`#src`
individually — the wrapper is present in the DOM from page load, which is what makes a live
region actually announce. Add `aria-hidden="true"` to `#netchip .dot` and to
`#admin-neticon` so the decorative glyphs (`■ ✖ ⚠ ⚡`) are not read aloud.

**Fix 4 — Label the three unnamed fields (SC 3.3.2 + 4.1.2, Level A).**

```html
<!-- index.html:343 — currently has no name of any kind -->
<input class="rvedit" id="rvqedit" style="display:none" aria-label="Question wording">

<!-- index.html:305 -->
<textarea id="ans" aria-label="Your answer, from memory" placeholder="…"></textarea>

<!-- index.html:451 -->
<input id="adminq" aria-label="Ask the corpus a question" placeholder="…">
```

**Fix 5 — Associate the submit hint (SC 1.3.1, Level A).**

```html
<button class="primary" id="submit" disabled aria-describedby="submithint">Submit answer</button>
```

**Fix 6 — Repair the publication tab names and state (SC 4.1.2, Level A).** The count span
currently welds onto the pub id, producing `"MCDP 18/74"`. In `renderPubs()`:

```js
'<button class="pubtab' + (state.pub === p.pub_id ? " on" : "") + '"' +
  ' role="tab" aria-selected="' + (state.pub === p.pub_id) + '"' +
  ' aria-label="' + esc(p.pub_id) + ', ' + p.attempted + ' of ' + p.n_items + ' practised"' +
  ' data-pub="' + esc(p.pub_id) + '">' + esc(p.pub_id) +
  '<span class="n" aria-hidden="true">' + p.attempted + "/" + p.n_items + "</span></button>"
```

and set `role="tablist"` on `#pubtabs`. If you would rather not take on full tablist
keyboard semantics (arrow-key roving tabindex), use `aria-pressed` on plain buttons instead
of `role="tab"`/`aria-selected` — it is conformant, simpler, and needs no keyboard work.

**Fix 7 — Raise `--line` where it forms a control boundary (SC 1.4.11, Level AA).** Do not
change `--line` globally, or every card gains a hard outline. Add one token:

```css
--line:         #34332e;   /* keep — decorative card borders and dividers (exempt) */
--line-control: #6c6a60;   /* NEW — 3.30:1 on --surface-1, 3.03:1 on --surface-2 */
```

then swap `var(--line)` → `var(--line-control)` on exactly these rules: `textarea`
(`:88`), `.rvedit` (`:174`), `.askbox input` (`:256`), `.ghost` (`:57`),
`.confbtns button` (`:96`), `.chip` (`:48`), `.duechip` (`:216`), `.pubtab` (`:239`).
For `.danger` (`:181`), replace `rgba(208,59,59,.5)` with a solid `#8f3636` or higher — at
50% alpha over `--surface-1` it measures 1.79:1.

**Fix 8 — Make the header wrap (SC 1.4.10, Level AA).** One declaration at `index.html:42`:

```css
header { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; row-gap: 10px; ... }
```

This is the entire reflow failure — `<main>` already reflows correctly at 320px.

**Fix 9 — Do not fully remove the outline on inputs (SC 2.4.7 hardening).** The current
border-colour swap is a measured 3.48:1 state change and technically passes, but it is
colour-only and identical in shape to the unfocused state. Replace the three `outline:none`
rules (`:90`, `:176`, `:258`) with a ring that adds a shape:

```css
textarea:focus-visible, .rvedit:focus-visible, .askbox input:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-color: var(--accent);
}
```

### Structural

**Fix 10 — Make publication cards real controls (SC 2.1.1, Level A).** The `<div onclick>`
must become a focusable element. The minimal change is to emit a `<button>` and let the CSS
follow:

```js
'<button class="pubcard" type="button" data-pub="' + esc(p.pub_id) + '">' + …  + '</button>'
```

```css
.pubcard { display: flex; align-items: center; gap: 16px; padding: 15px 0;
           border-bottom: 1px solid var(--line-soft); cursor: pointer; min-width: 0;
           width: 100%; background: none; border-radius: 0; text-align: left;
           font: inherit; color: inherit; }
```

`button { border: 0 }` at `:35` already removes the default border, and the existing
`.pubcard:hover .pcname` rule still applies. Adding `role="button" tabindex="0"` plus a
keydown handler to the `<div>` is the alternative, but it is strictly more code and more
things to get wrong — the native element is the better answer.

**Fix 11 — Make the drawer a real dialog (SC 2.4.3, Level A).** Four related problems:
offscreen tab stops when closed, no focus move on open, no focus trap, no focus restore.
The cheapest correct fix is to stop rendering the drawer when it is closed, which alone
removes the four offscreen tab stops:

```css
.drawer { … visibility: hidden; transition: transform .22s ease, visibility 0s .22s; }
.drawer.open { transform: translateX(0); visibility: visible; transition: transform .22s ease; }
```

`visibility:hidden` removes descendants from the tab order and the accessibility tree while
keeping the slide animation. Then add the dialog semantics and focus handling in
`openDrawer()`:

```html
<aside class="drawer" id="drawer" role="dialog" aria-modal="true" aria-label="How it works">
```

```js
let lastFocus = null;
function openDrawer(open) {
  if (open) lastFocus = document.activeElement;
  $("#drawer").classList.toggle("open", open);
  $("#scrim").classList.toggle("open", open);
  document.querySelector("main").toggleAttribute("inert", open);
  document.querySelector("header").toggleAttribute("inert", open);
  if (open) { $("#closeadmin").focus(); pollScore(); pollTelem(); loadCorpus(); loadGaps(); loadReviewStats(); }
  else if (lastFocus) { lastFocus.focus(); lastFocus = null; }
}
```

`inert` on `main` and `header` gives a real focus trap with no keydown-cycling code, and is
supported in all current browsers. Note the same `visibility` treatment is *not* needed for
the `<section>` views — `display:none` already handles those correctly.

**Fix 12 — Smaller structural tidy-ups.** Not conformance blockers, but worth doing in the
same pass: promote the drawer's `"How it works"` `<div class="brand">` to an `<h2>` so the
dialog has a heading; make `#rvq` (the review question) an `<h2>` rather than a `<div>`;
wrap the keyhint letters as `<span class="keyhint" aria-hidden="true">A</span>` and add
`aria-keyshortcuts="a"` to `#rvapprove` (etc.) so the accessible name reads `"Approve"`
rather than `"Approve A"`; and update `document.title` in `show()` so each view is
distinguishable in browser history and to screen-reader window announcements.

---

## Where four-feet legibility and WCAG pull in different directions

Mostly, they don't — and that is the useful finding. The brief's legibility requirement
produced a 17px base, a 27px question, and a 27px `h1`, and those large sizes are exactly
why the body text scores 17.94:1. The two requirements are aligned across most of the
interface. Three places are worth calling out.

**1. The one place they genuinely conflict: small status badges on a dark ground.**
`.state.mastered` (12px/600) and `.flag.UNGROUNDED_KEY_POINT` (13px) fail at 4.42:1 and
3.25:1. The obvious fix — make them bigger — is the *wrong* fix here, because the section
list renders 37 rows and the flag row can carry four badges; enlarging them wrecks the
density that makes those screens scannable. So these must be fixed by colour, which means
lightening `--good` and `--critical`. That is a real cost: a lighter red reads less like a
red alert at four feet, and the palette comment at `index.html:20` says the status colours
are "reserved, never decorative". The resolution is to keep the base tokens for fills and
dots and add text-tier variants used only for small text:

```css
--good:          #0ca30c;   /* unchanged — fills, dots, .ok button, .calbar */
--critical:      #d03b3b;   /* unchanged — fills, dots */
--good-text:     #0daf0d;   /* NEW — 5.05:1 on the good tint, 6.11:1 on --surface-1 */
--critical-text: #ff5757;   /* NEW — 5.02:1 on the crit tint, 5.77:1 on --surface-1 */
```

Apply `--good-text` to `.state.mastered` (`:153`) and `--critical-text` to
`.flag.UNGROUNDED_KEY_POINT` (`:167`) and `.histrow .v.incorrect` (`:249`). The saturated
originals survive everywhere they carry meaning at size.

**2. Where the two requirements agree, and the brief actually supplies the fix.**
Five of the eight contrast failures are 17px bold text — `.primary`, `.ok`, `.danger`,
`.verdict.correct`, `.verdict.incorrect`. At 17px they need 4.5:1 and miss. WCAG's
large-text threshold is 18.66px bold, so **raising them to 19px moves them to the 3:1
requirement, which all five already meet** (3.64, 3.35, 3.73, 4.42, 3.25 — all ≥3:1):

```css
.primary { … font-size: 19px; }              /* was 17px */
.ok      { … font-size: 19px; }              /* inherits 17px from body */
.danger  { … font-size: 19px; font-weight: 700; }  /* was 600 — WCAG requires ≥700 for "bold" */
.verdict { … font-size: 19px; }              /* was 17px */
```

This is the single most satisfying fix in the report: it resolves five WCAG failures *and*
makes the primary action buttons and the grading verdict more legible from four feet. The
alternative — darkening `--accent` to `#3277ca` and `--good` for white text — fixes the
ratio but makes the main call-to-action dimmer on a dark screen, which is the wrong
direction for the brief. **Prefer the size bump.**

**3. Where the brief is unmet but WCAG is silent.** A cluster of metadata text sits at
11–13px: `.caltag` (11px), `.pair .l` (11px), `.brand small` (12px), `#railcap` (12px),
`.keyhint` (12px), `.sec .state` (12px), `h2` (13px), `.hint` (13px). All of it passes
contrast comfortably at 5.16:1 — WCAG has no minimum font size, so none of it is a finding.
But 11px grey text is not readable from four feet on an external monitor, so this is a
**brief failure, not a WCAG failure**, and it will not show up in any automated scan.
Raising the 11px and 12px tiers to 13–14px would cost nothing in conformance terms and is
the change most likely to be noticed by the actual users standing in front of the screen.

---

## Summary

- **Findings: 10 WCAG 2.1 criteria fail — 6 at Level A, 4 at Level AA.**
  - Level A: 1.3.1, 2.1.1, 2.1.4, 2.4.3, 3.3.2, 4.1.2
  - Level AA: 1.4.3, 1.4.10, 1.4.11, 4.1.3
- **19 criteria were tested and pass**, including the two that were most suspected:
  `--text-3` contrast (5.16:1) and single-key shortcuts firing in text fields (they do not).
- **Three highest-value fixes:**
  1. **Fix 1** — modifier guard on the review shortcuts. `Ctrl+A` approving a doctrine
     question is a data-integrity bug wearing an accessibility costume, and it is a
     three-line change.
  2. **Fixes 3 + 4 + 5** — live regions and accessible names, ~10 attributes total. This is
     the difference between "a screen reader user can hear the grading result" and "cannot".
  3. **Fix 7 + the 19px bump in tension note 2** — one new colour token and four
     `font-size` declarations clear all four 1.4.11 failures and five of the eight 1.4.3
     failures, while making the interface *more* legible at four feet, not less.
- **Section 508 conformant today: NO.**

*Audited live via Playwright/Chromium against the running instance with all non-GET
requests blocked at the network layer; contrast computed from the source custom properties
with alpha overlays composited against their real backdrops; `.pubcard`, `.pubtab`, and the
review flag badges rendered through the application's own render functions because the live
dataset (one publication, empty review queue) does not exercise those code paths.*
