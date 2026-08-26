"""Unit tests for src/common/chunk_text.py.

This module is imported by BOTH the index builder and the retrieval path. If the two
ever disagreed about what text represents a chunk, dense scores would be computed
against one thing and reranked against another, and the abstention threshold would be
tuned on a moving target. push.sh copies this one file into both ingest/ and retrieval/
on the device, so these tests are the only place the shared contract is stated.

The measurement it exists for, on the correct chunk for "How does MCDP 1 define
friction?":

    text alone ............ -2.42   (negative: judged irrelevant)
    text with header ...... +3.39   (a 5.8-point swing)
"""
import chunk_text


def chunk(**kw):
    base = {"pub_id": "MCDP 1", "pub_title": "Warfighting", "chapter": 1,
            "chapter_title": "The Nature of War", "section": "Friction",
            "text": "Friction is the force that makes the apparently easy so difficult."}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# retrieval_header
# ---------------------------------------------------------------------------

def test_header_carries_the_publication_identifier():
    # The whole point: users name the publication -- "What does MCDP 1 say about
    # friction?" -- but the identifier lives in metadata, not prose. A cross-encoder only
    # sees the text it is given, so an unmatched "MCDP 1" counts as evidence AGAINST
    # relevance until the header puts it in the text.
    assert chunk_text.retrieval_header(chunk()) == \
        "MCDP 1, Warfighting | Chapter 1: The Nature of War | Friction"


def test_pub_title_is_not_repeated_when_it_equals_the_id():
    # Several publications carry no distinct title. "MCDP 6, MCDP 6" would waste the
    # reranker's 512-token budget on a duplicate and dilute the real signal.
    assert chunk_text.retrieval_header(
        chunk(pub_title="MCDP 6", pub_id="MCDP 6", chapter=None, chapter_title=None,
              section=None)) == "MCDP 6"


def test_chapter_title_without_a_number_omits_the_chapter_prefix():
    # Front matter and appendices have titles but no chapter ordinal. "Chapter None:
    # Foreword" would be fed to the embedding model verbatim.
    h = chunk_text.retrieval_header(chunk(chapter=None, chapter_title="Foreword",
                                          section=None))
    assert h == "MCDP 1, Warfighting | Foreword"
    assert "None" not in h


def test_missing_structure_degrades_to_the_publication_alone():
    h = chunk_text.retrieval_header(chunk(chapter=None, chapter_title=None, section=None))
    assert h == "MCDP 1, Warfighting"


def test_header_of_a_chunk_with_no_metadata_is_empty():
    # Not hypothetical: format_citation() suppresses unusable sections, and a chunk can
    # reach the index with nothing but text. The header must then be empty so
    # retrieval_text() falls back to body-only rather than prepending a stray separator.
    assert chunk_text.retrieval_header({"text": "Some prose."}) == ""


def test_section_is_stringified():
    # Bookmark titles are strings, but a numeric section survives from some outlines.
    # " | ".join() would raise on an int.
    assert chunk_text.retrieval_header(
        chunk(chapter=None, chapter_title=None, section=7)).endswith("| 7")


# ---------------------------------------------------------------------------
# retrieval_text
# ---------------------------------------------------------------------------

def test_header_is_prepended_to_the_body():
    c = chunk()
    out = chunk_text.retrieval_text(c)
    assert out == chunk_text.retrieval_header(c) + "\n" + c["text"]
    assert out.endswith(c["text"])


def test_body_only_when_there_is_no_header():
    # No leading newline and no empty first line: the reranker would spend tokens on it.
    c = {"text": "Some prose."}
    assert chunk_text.retrieval_text(c) == "Some prose."


def test_explicit_body_overrides_the_chunk_text():
    # build_index.py embeds sliding WINDOWS over a long chunk, passing each window as
    # `body` while keeping the chunk's own header. If this argument were ignored, every
    # window of a chunk would embed identically and the windowing would be a no-op.
    c = chunk()
    out = chunk_text.retrieval_text(c, body="a single window of the paragraph")
    assert out.endswith("\na single window of the paragraph")
    assert c["text"] not in out


def test_empty_string_body_is_honoured_rather_than_falling_back():
    # `body if body is not None else ...` -- an empty window must stay empty. Truth-value
    # testing here would silently substitute the full chunk text and desynchronise the
    # vec_map rows from what was actually embedded.
    c = chunk()
    assert chunk_text.retrieval_text(c, body="") == chunk_text.retrieval_header(c) + "\n"


def test_missing_text_key_does_not_raise():
    # Both callers pass raw sqlite rows. A NULL text column must produce an empty string,
    # not a TypeError in the middle of an index build.
    assert chunk_text.retrieval_text({"text": None}) == ""
    assert chunk_text.retrieval_text({}) == ""


def test_indexing_and_retrieval_produce_identical_text():
    # The contract the module docstring exists to enforce, stated as an assertion: the
    # same chunk fed through the same function twice must be byte-identical, because one
    # call happens in build_index.py and the other in hybrid.py.
    c = chunk()
    assert chunk_text.retrieval_text(dict(c)) == chunk_text.retrieval_text(dict(c))
