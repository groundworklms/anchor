"""Unit tests for src/ingest/chunker.py.

Every case here corresponds to a metadata failure that was actually measured on this
corpus. The citation is the product: a chunk with confidently wrong chapter/section
metadata is worse than one with none, because a Marine will open the publication at the
page it names and not find what it claims. These functions decide that metadata, so they
are the ones worth pinning.
"""
import pytest

import chunker


def line(text, size, bold=False, font="", x=72.0, y=100.0, x1=300.0):
    """A page_lines() row. Keys must match what page_lines() actually emits."""
    return {"text": text, "x": x, "x1": x1, "y": y,
            "size": size, "bold": bold, "font": font}


# ---------------------------------------------------------------------------
# font_family
# ---------------------------------------------------------------------------

def test_font_family_collapses_weight_and_style():
    # The family branch of looks_like_heading compares a heading's font to the BODY
    # font. If 'Arial-BoldMT' and 'Arial-ItalicMT' read as different families, every
    # bold or italic run inside a paragraph looks like a font change and gets promoted
    # to a heading.
    assert chunker.font_family("Arial-BoldMT") == "arial"
    assert chunker.font_family("Arial-ItalicMT") == "arial"
    assert chunker.font_family("Arial-BoldMT") == chunker.font_family("Arial-ItalicMT")


def test_font_family_strips_subset_prefix():
    # PDF font subsetting prefixes a six-letter tag. Without stripping it, the same font
    # subset differently on two pages compares unequal and the heading rule fires at
    # random.
    assert chunker.font_family("ABCDEF+Arial-BoldMT") == "arial"
    assert chunker.font_family("QWERTY+TimesNewRomanPSMT") == "timesnewromanpsmt"


def test_font_family_distinguishes_real_families():
    # MCDP 1-0 is Arial heads on a Times body: this inequality is the entire signal.
    assert chunker.font_family("Arial-BoldMT") != chunker.font_family("TimesNewRomanPSMT")


def test_font_family_of_missing_name_is_empty():
    # page_lines() emits font="" when a line has no fonted spans, and body_font can be
    # None. Neither may raise -- this runs over every line of thirteen publications.
    assert chunker.font_family(None) == ""
    assert chunker.font_family("") == ""


# ---------------------------------------------------------------------------
# outline_has_sections
# ---------------------------------------------------------------------------

def test_outline_has_sections_rejects_mcdp_1_0():
    # Real shape: 24 level-1 markers plus two level-2 entries that belong to an appendix
    # PDF embedded whole and point at *its* page 1. Trusting that outline labelled all
    # 712 chunks of the publication "Warfighting Functions" -- which is also the header
    # fed to the embedding model -- and degraded retrieval on 17% of the corpus.
    toc = [(1, "Chapter %d" % i, i + 1) for i in range(24)]
    toc += [(2, "APPENDIX B", 300), (2, "Warfighting Functions", 300)]
    assert chunker.outline_has_sections(toc) is False


def test_outline_has_sections_accepts_tc_3_22_9():
    # The other side of the measured split: 1815 section entries against 12 chapters.
    # This outline is authoritative and must be kept.
    toc = [(1, "Chapter %d" % i, i + 1) for i in range(12)]
    toc += [(2, "Section %d" % i, 20 + i) for i in range(1815)]
    assert chunker.outline_has_sections(toc) is True


def test_outline_has_sections_on_empty_outline():
    # The scanned 1996-97 MCDPs ship no bookmarks at all. The predicate must answer "no
    # sections" rather than divide by zero or raise on an empty Counter.
    assert chunker.outline_has_sections([]) is False


def test_outline_has_sections_counts_levels_below_two_as_sections():
    # A three-level outline (chapter / section / subsection) still names sections. The
    # test is level >= 2, not level == 2; deep entries must not fall out of the count.
    toc = [(1, "Chapter 1", 1), (3, "1.1.1 Something", 2), (3, "1.1.2 Else", 3)]
    assert chunker.outline_has_sections(toc) is True


# ---------------------------------------------------------------------------
# looks_like_heading
# ---------------------------------------------------------------------------

def test_all_caps_heading_at_body_size_is_detected():
    # MCDP 1's "UNCERTAINTY" measures 11.90 against a body median of 11.40 -- a ratio of
    # 1.044, below the 1.06 size threshold. Missing it silently reattributed five
    # paragraphs of the Uncertainty section to Friction. Capitalisation, not size, has to
    # carry this.
    assert chunker.looks_like_heading(line("UNCERTAINTY", 11.9), 11.4) is True


def test_sentence_case_body_text_is_not_a_heading():
    # The negative half of the same rule: body prose at body size must never be
    # promoted, or every line break starts a new section.
    assert chunker.looks_like_heading(
        line("The nature of war is friction", 11.4), 11.4) is False


def test_line_ending_in_a_period_is_not_a_heading():
    # OCR of a short all-caps sentence is indistinguishable from a display heading
    # except for its terminal punctuation. Headings in these publications are not
    # punctuated, so a trailing '.' ',' ';' ':' is the cheapest reliable veto.
    assert chunker.looks_like_heading(line("FRICTION IS THE FORCE.", 11.9), 11.4) is False
    assert chunker.looks_like_heading(line("THE FOG OF WAR:", 11.9), 11.4) is False


def test_bold_at_115x_body_is_a_heading_even_in_title_case():
    # TCCC sets heads 16pt bold against a 12pt body in TITLE case, so the capitalisation
    # rule never fires on it. Without this branch every TCCC chunk carried section=None
    # -- the worst citations in the corpus, on the document where precision matters most
    # because its content is clinical.
    assert chunker.looks_like_heading(
        line("Basic Management Plan for Care Under Fire/Threat", 16.0,
             bold=True, font="Arial-BoldMT"), 12.0) is True


def test_font_family_change_at_exactly_body_size_is_a_heading():
    # MCDP 1-0's 11pt subsection heads are Arial-BoldMT at EXACTLY the Times body size,
    # so every size-based rule misses them. The family change is the only signal left.
    assert chunker.looks_like_heading(
        line("Warfighting Functions", 11.0, bold=True, font="Arial-BoldMT"),
        11.0, "TimesNewRomanPSMT") is True


def test_font_family_change_below_body_size_is_not_a_heading():
    # The same family change at 9pt is figure captions and table cells, which MCDP 1-0
    # sets in the heading font throughout. Requiring at least body size is what keeps
    # table furniture out of the section names.
    assert chunker.looks_like_heading(
        line("Figure 3 Continued", 9.0, bold=True, font="Arial-BoldMT"),
        11.0, "TimesNewRomanPSMT") is False


def test_body_font_none_disables_the_family_branch():
    # The scanned MCDPs report a single OCR font for the whole page, so there is no
    # family signal to be had and the caller passes body_font=None. The branch must then
    # be inert: this title-case line falls through to the capitalisation test and loses.
    assert chunker.looks_like_heading(
        line("Warfighting Functions", 11.0, bold=True, font="Arial-BoldMT"),
        11.0, None) is False


def test_too_short_or_too_long_lines_are_rejected():
    # A stray OCR'd initial and a full all-caps paragraph both satisfy the
    # capitalisation rule. Length is the only thing separating them from a real heading.
    assert chunker.looks_like_heading(line("A", 14.0), 11.4) is False
    assert chunker.looks_like_heading(line("WAR " * 40, 14.0), 11.4) is False


# ---------------------------------------------------------------------------
# is_navigation_chunk
# ---------------------------------------------------------------------------

def test_dot_leader_run_is_navigation():
    # MCDP 1-0's contents pages survive as ordinary paragraphs and chunk into runs like
    # this. They carry every keyword in the publication and none of its meaning, and one
    # eval question missed entirely because they crowded the real answer out of the
    # ranked list.
    text = "Assessment . . . . . . . . . . 4-7 Planning . . . . . . . . . . 4-9"
    assert chunker.is_navigation_chunk(text, None, None) is True


def test_nav_title_marks_the_chunk_regardless_of_prose():
    # Well-formed prose sitting under a "Table of Contents" heading is still front
    # matter. Either the chapter title or the section name is enough to condemn it.
    prose = "This publication is organised into four chapters and two appendices."
    assert chunker.is_navigation_chunk(prose, "Chapter 1", "Table of Contents") is True
    assert chunker.is_navigation_chunk(prose, "Glossary", None) is True


def test_low_alpha_ratio_marks_page_number_runs():
    # An index entry that survived without dot leaders: mostly digits and punctuation.
    assert chunker.is_navigation_chunk("4-7, 4-9, 5-1, 5-3, 6-11, 7-2", None, None) is True


def test_real_doctrine_prose_is_not_navigation():
    # The case that must NOT be caught. Over-filtering here deletes answers outright,
    # and it is a silent failure: the chunk simply never appears in any result.
    text = ("Friction is the force that makes the apparently easy so difficult. "
            "It is the atmosphere of war, and it exists on both sides.")
    assert chunker.is_navigation_chunk(text, "Chapter 1", "Friction") is False


# ---------------------------------------------------------------------------
# format_citation
# ---------------------------------------------------------------------------

def test_numbered_chapter_title_does_not_stutter():
    # MCDP 1-0's outline titles its chapters "Chapter 5", which rendered as
    # "Ch 5: Chapter 5". A citation a Marine reads aloud must not stutter.
    out = chunker.format_citation("MCDP 1-0", "2011", "Chapter 5", 5, None, 3, "5-7")
    assert out == "MCDP 1-0, (2011), Ch 5, para 3, p.5-7"
    assert "Chapter 5" not in out


@pytest.mark.parametrize("title", ["Chapter 5", "chapter 5", "Chap 5", "CHAPTER 5",
                                   "Chapter5", "  Chapter 5  "])
def test_numbered_chapter_variants_are_all_dropped(title):
    # The abbreviation, the casing and the stray whitespace all occur in real outlines,
    # and each one that slips through reintroduces the stutter.
    out = chunker.format_citation("MCDP 1-0", None, title, 5, None, 1, "5-1")
    assert out == "MCDP 1-0, Ch 5, para 1, p.5-1"


def test_named_chapter_title_is_kept():
    # The other half: a real chapter name must survive, or the citation loses the only
    # part of it a reader can navigate by from the printed table of contents.
    out = chunker.format_citation("MCDP 1", "1997", "The Nature of War", 1,
                                  "Friction", 2, "7")
    assert out == 'MCDP 1, (1997), Ch 1: The Nature of War, "Friction", para 2, p.7'


def test_unusable_section_is_suppressed_not_invented():
    # A cover-page "Distribution Statement A" heading is typographically real and
    # useless as a section name. The citation is judged on looking trustworthy as well as
    # being trustworthy, so the section is dropped and the rest still resolves.
    out = chunker.format_citation("MCWP 3-11.3", None, None, None,
                                  "Distribution Statement A", 1, "12")
    assert out == "MCWP 3-11.3, para 1, p.12"


def test_ocr_noise_section_is_suppressed():
    # Digit-dense OCR garbage must never be quoted back at a reader as a section name.
    out = chunker.format_citation("MCDP 1", None, "The Nature of War", 1,
                                  "F1UcTI0N 4-7 1-2", 2, "7")
    assert '"' not in out
    assert out == "MCDP 1, Ch 1: The Nature of War, para 2, p.7"


def test_citation_degrades_to_pub_and_paragraph():
    # Worst case: no edition, no chapter, no section, no printed page number. It must
    # still emit something a human can act on rather than a run of empty commas.
    assert chunker.format_citation("MCDP 6", None, None, None, None, 4, None) == \
        "MCDP 6, para 4"


@pytest.mark.parametrize("chapter_title,chapter", [
    ("The Nature of War", 1),
    ("Chapter 5", 5),
    (None, 3),
    (None, None),
])
def test_citation_always_names_the_publication_and_paragraph(chapter_title, chapter):
    # The invariant everything downstream leans on: the publication id comes first and
    # the paragraph ordinal is always present, whatever else is missing.
    out = chunker.format_citation("MCDP 1", None, chapter_title, chapter, None, 7, "12")
    assert out.startswith("MCDP 1, ")
    assert "para 7" in out


# ---------------------------------------------------------------------------
# strip_leading_endnotes
# ---------------------------------------------------------------------------
#
# The chunker glued endnote/reference fragments onto the HEAD of the next real paragraph,
# so 3 of MCDP 7's 13 items cited "MCDP 7, Ch 4: Notes" for doctrine that actually lives in
# MCDP 1 -- the wrong-publication attribution D-036 caught and D-045 saw the reranker score
# on. These cases are verbatim from the corpus. The metadata failure was measured; so is the
# fix. Two rules carry it, and both directions are pinned here: a leading citation is
# stripped, but an Army numbered paragraph and a numbered procedure list are NOT.

# The two real MCDP 7 quotes named in the incident. Curly quotes are what the OCR emits, so
# one case carries them to prove the split lands on the opening quote either way.
GLUED_IBID = ('7. Ibid., p. 1-6. “All actions in war take place in an atmosphere of '
              'uncertainty, or the “fog of war.” Uncertainty pervades battle in '
              'the form of unknowns about the enemy, about the environment.”')
GLUED_MAIN_EFFORT = ('6. MCDP 1, pp. 4-21–4-22. "Another important tool for providing '
                     'unity is the main effort. Of all the actions going on within our '
                     'command, we recognize one as the most critical to success."')
# Two endnote markers, then real prose -- the note whose own marker (18.) prefixes doctrine.
GLUED_TEMPO = ('17. Clausewitz, p. 194. 18. Tempo is often associated with a mental process '
               'known variously as the "decision cycle," "OODA loop," or "Boyd cycle" after '
               'John Boyd who pioneered the concept in his lecture, "The Patterns of '
               'Conflict." Boyd identified a four-step mental process.')


def test_glued_ibid_endnote_is_split_off():
    # `7. Ibid., p. 1-6.` is a Latin-abbreviation citation: its own period must be crossed
    # to reach the page ref, but the doctrine quote behind it must emerge clean.
    out = chunker.strip_leading_endnotes(GLUED_IBID)
    assert out.startswith("“All actions in war")
    assert "Ibid." not in out
    assert not out.lstrip()[0].isdigit()


def test_glued_page_range_endnote_is_split_off():
    out = chunker.strip_leading_endnotes(GLUED_MAIN_EFFORT)
    assert out.startswith('"Another important tool')
    assert "MCDP 1, pp." not in out
    assert not out.lstrip()[0].isdigit()


def test_two_endnote_markers_then_prose_is_split_off():
    # Both `17. Clausewitz, p. 194.` (a citation) and the bare `18.` (a marker on real
    # prose) must go, leaving the note's doctrine text.
    out = chunker.strip_leading_endnotes(GLUED_TEMPO)
    assert out.startswith("Tempo is often associated")
    assert "Clausewitz" not in out
    assert "17." not in out and "18." not in out


def test_imprint_terminated_citation_run_is_split_off():
    # D-036 item MCDP7-c4-463960f0f6-0: a bibliography line ending in a publisher imprint,
    # then a page-ref citation, then the real quoted doctrine.
    text = ('7. Malcolm Shepherd Knowles, Self-Directed Learning (New York: Cambridge '
            'Books, 1975). 8. MCDP 1, p. 4-18. "Mission tactics is just as the name '
            'implies: the tactics of assigning a subordinate mission without specifying '
            'how the mission must be accomplished."')
    out = chunker.strip_leading_endnotes(text)
    assert out.startswith('"Mission tactics')
    assert "Knowles" not in out and "p. 4-18" not in out


@pytest.mark.parametrize("text", [
    GLUED_IBID, GLUED_MAIN_EFFORT, GLUED_TEMPO,
])
def test_split_is_idempotent(text):
    # Re-chunking must be stable: stripping an already-stripped chunk changes nothing,
    # or ids would churn on every ingest (D-035).
    once = chunker.strip_leading_endnotes(text)
    assert chunker.strip_leading_endnotes(once) == once


@pytest.mark.parametrize("text", [
    "3-67. The Soldier assumes a stable firing position and applies steady pressure to "
    "the trigger until the weapon fires without disturbing the sight picture.",
    "C-11. Moving targets require the firer to establish a lead based on target speed, "
    "range, and the angle of movement relative to the firer.",
])
def test_army_numbered_paragraph_is_not_split(text):
    # `3-67.` and `C-11.` -- the digits follow a hyphen, and TC 3-22.9 alone opens 486
    # paragraphs this way. The `(?<![\w-])` lookbehind refuses them; they must come back
    # byte-identical or hundreds of real paragraphs would be mangled.
    assert chunker.strip_leading_endnotes(text) == text


@pytest.mark.parametrize("text", [
    "1. Orient the map using the compass. 2. Locate and mark your position on the map. "
    "3. Measure the magnetic azimuth to the unknown position; then convert to grid azimuth.",
    "1. Move up to the obstacle and make a full 90degree turn to the right (or left). "
    "2. Walk beyond the obstacle, keeping track of the distance in paces or meters.",
    "1. Begin planning. 2. Arrange for reconnaissance and coordination. 3. Make "
    "reconnaissance. 4. Complete the plan. 5. Issue the order. 6. Supervise.",
])
def test_numbered_procedure_list_is_not_split(text):
    # The land-nav drills and Care Under Fire steps open `1. ... 2. ...` with NO page
    # reference. The mandatory page-ref/imprint terminator is what keeps the strip off
    # them -- a run rule would delete exactly the content D-036 refused to lose. Note the
    # first case ends a step on "...the map." followed by "3." -- the `\b` in the page-ref
    # pattern is what stops that reading as a page ref "p. 3".
    assert chunker.strip_leading_endnotes(text) == text


def test_ordinary_doctrine_prose_is_byte_identical():
    # The no-op proof the blast-radius argument rests on: a normal paragraph that never
    # opened with a citation must be returned unchanged, character for character, so its
    # id and every human approval keyed to it survive re-chunking (D-035).
    text = ("Friction is the force that makes the apparently easy so difficult. It is the "
            "atmosphere of war, and it exists on both sides. Even the simplest act becomes "
            "hard when performed under the conditions of combat.")
    assert chunker.strip_leading_endnotes(text) == text


@pytest.mark.parametrize("text", [
    # A single bibliography entry (imprint, no glued prose).
    "6. MCDP 1, Warfighting (Washington, D.C., Headquarters US Marine Corps, June 1997) "
    "p. 3-6.",
    # A run of two bibliography entries.
    "1. Ryan Holiday, Ego is the Enemy (New York: Penguin Random House, 2016) p. 62. "
    "2. Oren Harari, The Leadership Secrets of Colin Powell (New York: McGraw-Hill, 2002) "
    "p. 164.",
    # An imprint entry followed by another whose author name begins with a capital -- the
    # bare-marker strip must NOT fire, because what follows is still a citation, not prose.
    "6. Carol S. Dweck, PhD, Mindset: The New Psychology of Success (New York: Random "
    "House, 2006). 1. Albin Krebs and Robert Mcg. Thomas, Jr., \"NOTES ON PEOPLE,\" New "
    "York Times, National edition, November 9, 1986.",
])
def test_pure_endnote_block_is_returned_as_is(text):
    # A chunk that is ONLY reference apparatus has no real paragraph to rescue. It is left
    # byte-identical rather than emptied: it must not crash, and changing it would need-
    # lessly re-key a chunk that was never the glued-prose defect.
    assert chunker.strip_leading_endnotes(text) == text


def test_empty_and_none_text_do_not_crash():
    # flush() can hand this an empty string; the guard must be inert, not raise.
    assert chunker.strip_leading_endnotes("") == ""
    assert chunker.strip_leading_endnotes(None) is None
