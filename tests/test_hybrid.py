"""Unit tests for the pure halves of src/retrieval/hybrid.py.

Two functions here need neither the index nor a model server:

  fts_query  -- turns a user question into an FTS5 MATCH expression
  rrf_fuse   -- merges ranked lists by reciprocal rank

The FTS5 tests build a real in-memory FTS5 table and execute the generated expression
against it. That is deliberate: asserting on the STRING only proves what the function
returns, not that sqlite will accept it, and the failure being guarded against is a
sqlite3.OperationalError raised on a user's question at query time. sqlite3 ships with
FTS5 in CPython, so this stays offline and instant.
"""
import sqlite3
import urllib.error

import pytest

import hybrid


@pytest.fixture
def fts():
    """A real FTS5 table, so 'is this a valid MATCH expression' is answered by sqlite.

    The schema mirrors src/ingest/build_index.py: tokenize='porter unicode61', over
    chunk_text.retrieval_text() -- provenance header PLUS body, not the bare body. That
    became load-bearing rather than cosmetic when publication identifiers started being
    emitted as phrases: whether "MCDP 1-0" is findable at all depends on unicode61
    splitting the hyphen so the header indexes as adjacent tokens mcdp/1/0. A fixture on
    the default tokeniser, or on headerless rows, would prove nothing about the index the
    device actually queries.
    """
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE chunks_fts "
               "USING fts5(text, tokenize='porter unicode61')")
    db.executemany("INSERT INTO chunks_fts(text) VALUES (?)", [
        ("MCDP 1-0, Marine Corps Operations | Chapter 5: Offensive Operations\n"
         "Friction is the force that makes the apparently easy so difficult in war.",),
        ("MCDP 6, Command and Control | Chapter 2: Understanding Command and Control\n"
         "Commander's intent describes the desired end state of the operation.",),
    ])
    return db


def run_match(db, expr):
    return db.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?",
                      (expr,)).fetchall()


def run_ranked(db, expr):
    """rowids best-first -- the order bm25_search() actually hands to RRF."""
    return [r[0] for r in db.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
        "ORDER BY bm25(chunks_fts)", (expr,)).fetchall()]


# ---------------------------------------------------------------------------
# fts_query -- neutralising FTS5 syntax
# ---------------------------------------------------------------------------

def test_ordinary_question_becomes_a_quoted_or_expression():
    # Every token is stripped of punctuation and quoted so the query is data, never
    # grammar. The trailing '?' is not the literal the user meant and must not survive.
    assert hybrid.fts_query("What is friction in war?") == \
        '"What" OR "is" OR "friction" OR "in" OR "war"'


@pytest.mark.parametrize("question", [
    'What does MCDP 1 say about "friction"?',          # bare quote: FTS5 syntax error
    "What is a commander's intent?",                   # apostrophe
    "friction AND (fog OR NOT uncertainty)",           # operators typed by the user
    "NEAR(friction fog, 5)",                           # a NEAR expression
    "friction -war",                                   # leading hyphen
    "^friction fog*",                                  # column filter and prefix
    "((((",                                            # unbalanced parentheses
    'friction" OR "1"="1',                             # quote-injection shaped input
    "{fog} : friction",                                # column-filter syntax
    "friction\\fog",                                   # backslash
    "what -- exactly -- is friction?",                 # em-dash run
    "1-0 2.3.4 -- MCDP 1-0 para 2",                    # a citation pasted as a question
    # The same attacks wearing a digit, because a digit-bearing word now takes a
    # different branch and builds a multi-token phrase rather than one quoted word.
    'MCDP 1" OR "1"="1',                               # injection wearing an identifier
    "NEAR(MCDP 1-0, 5)",                               # NEAR around an identifier
    "^MCDP 1-0",                                       # column filter on an identifier
    "MCDP 1-0*",                                       # prefix operator on an identifier
    "MCDP 1-0 : 5",                                    # column-filter syntax with digits
    "MCDP 1-0\\5",                                     # backslash inside an identifier
    "_ 5",                                             # phrase whose first token vanishes
    "--5--",                                           # digits wrapped in operators
    "MCDP " + "1-" * 60 + "0",                         # a 61-token phrase
])
def test_adversarial_input_produces_an_expression_sqlite_accepts(fts, question):
    # The point of this test is that it must not raise. User text goes straight into a
    # MATCH expression, where bare punctuation is FTS5 grammar -- a stray quote or hyphen
    # is a syntax error that would surface as a 500 on a perfectly reasonable question.
    expr = hybrid.fts_query(question)
    if expr is None:
        return                       # nothing searchable: bm25_search short-circuits
    run_match(fts, expr)             # raises sqlite3.OperationalError if malformed


def test_quotes_in_the_question_cannot_break_out_of_the_quoting():
    # Injection-shaped input specifically: the quoting only works because the punctuation
    # is REMOVED before the quotes are added, not escaped. If a quote ever survived into
    # a term, the expression would terminate early and the rest would parse as grammar.
    expr = hybrid.fts_query('friction" OR "1"="1')
    for term in expr.split(" OR "):
        assert term.startswith('"') and term.endswith('"')
        assert '"' not in term[1:-1]


def test_operator_words_are_quoted_into_literals(fts):
    # AND / OR / NOT / NEAR typed by a user are search terms, not operators. Quoting them
    # is what stops "friction AND fog" from being parsed as a conjunction the user never
    # asked for -- and what stops a dangling "NOT" from being a syntax error.
    expr = hybrid.fts_query("friction AND NOT NEAR fog")
    assert expr == '"friction" OR "AND" OR "NOT" OR "NEAR" OR "fog"'
    run_match(fts, expr)


def test_all_punctuation_returns_none(fts):
    # bm25_search() returns [] on None rather than executing a MATCH. An empty string
    # here would be a syntax error, and a bare '""' would match nothing but still cost a
    # query.
    assert hybrid.fts_query("???") is None
    assert hybrid.fts_query("!!! ... ---") is None
    assert hybrid.fts_query("") is None
    assert hybrid.fts_query("   ") is None


def test_single_character_tokens_are_dropped():
    # len(t) > 1. Single letters are noise in BM25 and blow up the term count on OCR'd
    # text. This still holds for every token that is not part of a digit-bearing run --
    # see test_publication_identifiers_survive_as_phrases for the carve-out and the
    # measurement that forced it.
    assert hybrid.fts_query("a b cd e fg") == '"cd" OR "fg"'


def test_publication_identifiers_survive_as_phrases(fts):
    # This was test_numeric_identifier_tokens_do_not_survive, and it pinned the OPPOSITE:
    # "MCDP 1-0" split to MCDP/1/0, the len > 1 filter ate both digits, and the BM25 half
    # could not see the publication number a Marine had just typed -- all 2,892 MCDP
    # chunks scored alike and the dense half carried the disambiguation alone
    # (chunk_text.py: +3.39 with the header, -2.42 without). That is now fixed rather
    # than merely documented. A digit-bearing word becomes a quoted PHRASE glued to the
    # plain word in front of it, and the plain word is still emitted on its own, so the
    # change is additive: BM25 still finds everything it found before.
    assert hybrid.fts_query("MCDP 1-0 chapter 5") == \
        '"MCDP" OR "MCDP 1 0" OR "chapter" OR "chapter 5"'
    assert hybrid.fts_query("How does MCDP 1 define friction?") == \
        '"How" OR "does" OR "MCDP" OR "MCDP 1" OR "define" OR "friction"'
    run_match(fts, hybrid.fts_query("MCDP 1-0 chapter 5"))


@pytest.mark.parametrize("typed, phrase", [
    ("MCDP 1", "MCDP 1"),                  # every pub_id in corpus/chunks.jsonl,
    ("MCDP 1-0", "MCDP 1 0"),              # as a Marine would type it
    ("MCDP 1-1", "MCDP 1 1"),
    ("MCDP 1-2", "MCDP 1 2"),
    ("MCDP 1-3", "MCDP 1 3"),
    ("MCDP 2", "MCDP 2"),
    ("MCDP 7", "MCDP 7"),
    ("MCWP 3-11.3", "MCWP 3 11 3"),
    ("TC 3-22.9", "TC 3 22 9"),
    ("chapter 5", "chapter 5"),            # and the reference shapes around them
    ("paragraph 4-21", "paragraph 4 21"),
    ("4-21", "4 21"),                      # a bare paragraph ref, nothing in front of it
    ("p. 5", "p 5"),                       # 'p' is kept for the phrase even though it is
])                                         # too short to be a term of its own
def test_every_identifier_shape_in_this_corpus_becomes_a_phrase(fts, typed, phrase):
    # TCCC is deliberately absent from this list: it carries no digit, so the length
    # filter never touched it and it still travels as an ordinary quoted token.
    expr = hybrid.fts_query(typed)
    assert f'"{phrase}"' in expr
    run_match(fts, expr)                   # and sqlite accepts every one of them


def test_the_phrase_can_actually_match_the_index(fts):
    # The question that decided whether this change was worth making at all. FTS5
    # tokenises a phrase with the same tokeniser it indexed with, so "MCDP 1-0" typed in
    # a query and "MCDP 1-0," sitting in a header both become the run mcdp/1/0 and the
    # phrase spans it. A phrase that could never match would have been worse than the old
    # behaviour and the honest answer would have been to leave the filter alone. Checked
    # against the full 4,230-chunk corpus BEFORE the change was written: "MCDP 1 0"
    # returns exactly the 712 MCDP 1-0 chunks, "MCWP 3 11 3" exactly the 668 MCWP 3-11.3
    # chunks -- no empty result, no stray family members.
    assert run_match(fts, '"MCDP 1 0"') == [(1,)]
    assert run_match(fts, '"MCDP 6"') == [(2,)]
    assert run_match(fts, hybrid.fts_query("MCDP 1-0")) != []


def test_the_identifier_alone_decides_the_bm25_order(fts):
    # The defect in one assertion. Both rows are MCDP, so the bare word cannot separate
    # them: before this change both queries produced the identical expression '"MCDP"'
    # and BM25 contributed nothing toward which publication was meant. The phrase is far
    # rarer than the bare word, so it carries the IDF that lifts the named publication to
    # rank 1. Measured on the real corpus over the 11 eval questions that name a
    # publication: mean top-50 BM25 chunks belonging to the NAMED pub went 23.4 -> 26.1,
    # MCDP 6 went 30 -> 42, and MCDP 5 went 17 -> 33 with its first correct chunk moving
    # from rank 11 to rank 5.
    assert run_ranked(fts, hybrid.fts_query("MCDP 1-0"))[0] == 1
    assert run_ranked(fts, hybrid.fts_query("MCDP 6"))[0] == 2


def test_a_short_identifier_narrows_but_does_not_pin(fts):
    # Accepted limit, recorded rather than hidden. FTS5 has no anchor for the end of a
    # token run, so the phrase "MCDP 1" is a positional PREFIX of the indexed "MCDP 1-0"
    # and matches the whole 1-x family. On the real corpus that is 1,781 chunks where
    # MCDP 1 itself holds 241 -- a real narrowing from the 2,892 that bare "MCDP" matched,
    # but not the pin that a longer identifier gets. MCDP 1 and its 1-x supplements are
    # the only place in this corpus where it bites.
    assert run_match(fts, '"MCDP 1"') == [(1,)]     # the MCDP 1-0 row, not an MCDP 1 row


def test_bare_single_digits_are_still_dropped(fts):
    # The length filter's original job, untouched. On the real corpus a bare "1" matches
    # 2,278 of 4,230 chunks and a bare "5" matches 540: pure noise in BM25, and the
    # reason splitting an identifier into loose tokens was never an option. A digit earns
    # a place only when something precedes it in the same run.
    assert hybrid.fts_query("5") is None
    assert hybrid.fts_query("5 5 5") is None
    assert hybrid.fts_query("-5") is None
    assert hybrid.fts_query("chapter 5") == '"chapter" OR "chapter 5"'


def test_punctuation_between_a_word_and_a_number_breaks_the_adjacency(fts):
    # A phrase asserts the two tokens sit NEXT TO each other in the index. "MCDP -- 1" is
    # not that claim, and gluing across the dash would invent an adjacency the user never
    # typed and no header contains -- a phrase matching nothing, the one outcome this
    # change had to avoid.
    assert hybrid.fts_query("MCDP -- 1") == '"MCDP"'
    run_match(fts, hybrid.fts_query("MCDP -- 1"))


def test_underscores_survive_as_word_characters(fts):
    # \w includes '_', so an underscore run becomes a term the FTS5 tokeniser reduces to
    # nothing. It matches nothing, but it must not error.
    expr = hybrid.fts_query("___ friction")
    assert run_match(fts, expr)              # 'friction' still finds the chunk


def test_a_real_question_still_retrieves(fts):
    # The neutralising must not be so aggressive that nothing matches -- a "safe" query
    # that never hits is the same outage with a different cause.
    rows = run_match(fts, hybrid.fts_query("What is friction in war?"))
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# rrf_fuse
# ---------------------------------------------------------------------------

def test_rrf_scores_are_the_sum_of_reciprocal_ranks():
    # RRF operates on ranks precisely so incommensurable scores never meet: BM25 returns
    # lower-is-better, sqlite-vec returns cosine distance. The exact arithmetic is pinned
    # because the abstention threshold downstream is tuned against these magnitudes.
    fused = hybrid.rrf_fuse({"bm25": [(10, -3.2), (20, -2.9)],
                             "dense": [(20, 0.11), (30, 0.42)]}, k=60)
    scores = {cid: sc for cid, sc, _ in fused}
    assert scores[10] == pytest.approx(1 / 61)
    assert scores[30] == pytest.approx(1 / 62)
    assert scores[20] == pytest.approx(1 / 62 + 1 / 61)


def test_agreement_between_halves_outranks_a_first_place_in_one():
    # The behaviour the fusion is chosen for. Chunk 20 is second in both lists and must
    # beat chunk 10, which is first in one and absent from the other.
    fused = hybrid.rrf_fuse({"bm25": [(10, -3.2), (20, -2.9)],
                             "dense": [(30, 0.10), (20, 0.11)]}, k=60)
    assert [cid for cid, _, _ in fused][0] == 20


def test_found_by_records_every_contributing_list():
    # search() surfaces this as `found_by` and the debug UI prints it. Losing a source
    # name makes it impossible to tell whether a bad result came from BM25 or the vectors.
    fused = hybrid.rrf_fuse({"bm25": [(10, -3.2), (20, -2.9)],
                             "dense": [(20, 0.11)]}, k=60)
    seen = {cid: src for cid, _, src in fused}
    assert seen[20] == ["bm25", "dense"]
    assert seen[10] == ["bm25"]


def test_results_are_sorted_best_first():
    # search() slices this list positionally to build the rerank input, so the ordering
    # is load-bearing rather than cosmetic.
    fused = hybrid.rrf_fuse({"bm25": [(i, 0.0) for i in range(1, 6)],
                             "dense": [(5, 0.0), (4, 0.0)]}, k=60)
    scores = [sc for _, sc, _ in fused]
    assert scores == sorted(scores, reverse=True)
    assert [cid for cid, _, _ in fused][:2] == [5, 4]


def test_fusing_an_empty_half_is_the_other_half():
    # A question with no searchable terms yields no BM25 hits at all (fts_query returns
    # None). The dense half must still produce a usable ranking rather than an empty
    # result.
    fused = hybrid.rrf_fuse({"bm25": [], "dense": [(7, 0.1), (8, 0.2)]}, k=60)
    assert [cid for cid, _, _ in fused] == [7, 8]


def test_fusing_two_empty_halves_yields_nothing():
    # search() checks `if not fused` before building the SQL IN clause; an empty list
    # here is what stops it constructing "WHERE id IN ()".
    assert hybrid.rrf_fuse({"bm25": [], "dense": []}, k=60) == []


def test_k_damps_the_advantage_of_rank_one():
    # A larger k flattens the curve, which is the tunable that decides how much a single
    # confident half can dominate the fused order.
    small = hybrid.rrf_fuse({"a": [(1, 0.0), (2, 0.0)]}, k=1)
    large = hybrid.rrf_fuse({"a": [(1, 0.0), (2, 0.0)]}, k=1000)
    small_gap = small[0][1] - small[1][1]
    large_gap = large[0][1] - large[1][1]
    assert small_gap > large_gap


# ---------------------------------------------------------------------------
# rerank transport controls. The general retrieval path keeps its established
# long-running recovery policy; the scoped API opts into the bounded variant.
# ---------------------------------------------------------------------------

def test_rerank_defaults_keep_legacy_timeout_and_context_recovery(monkeypatch):
    calls = []

    def fake_post_json(url, payload, **kwargs):
        calls.append((payload["documents"], kwargs))
        if len(calls) == 1:
            raise urllib.error.HTTPError(url, 500, "context", {}, None)
        return {"results": [{"index": 0, "relevance_score": 4.0}]}

    monkeypatch.setattr(hybrid, "post_json", fake_post_json)
    assert hybrid.rerank("question", ["x" * 800]) == [(0, 4.0)]
    assert calls == [
        (["x" * 700], {"timeout": 300}),
        (["x" * 350], {"timeout": 300}),
    ]


def test_rerank_can_disable_retries_for_a_bounded_caller(monkeypatch):
    calls = []

    def fake_post_json(url, payload, **kwargs):
        calls.append((payload["documents"], kwargs))
        raise urllib.error.HTTPError(url, 500, "context", {}, None)

    monkeypatch.setattr(hybrid, "post_json", fake_post_json)
    with pytest.raises(urllib.error.HTTPError):
        hybrid.rerank("question", ["x" * 800], timeout=20, attempts=1,
                      retry_context_errors=False)
    # No smaller-document retry occurs, and post_json is told to make one request only.
    assert calls == [(["x" * 700], {"timeout": 20, "attempts": 1})]
