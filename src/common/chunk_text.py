"""Shared text preparation for retrieval.

Imported by BOTH the index builder and the retrieval path. If these two ever disagree
about what text represents a chunk, dense scores are computed against one thing and
reranked against another, and the abstention threshold is tuned on a moving target.
Keeping it in one module is the only way to guarantee they cannot drift.
"""

# Users ask doctrine questions by naming the publication -- "What does MCDP 1 say about
# friction?" -- but the publication identifier lives in a chunk's METADATA, not in its
# prose. A cross-encoder reranker only sees the text it is given, so it treats the
# unmatched "MCDP 1" as evidence AGAINST relevance.
#
# Measured on the correct chunk for "How does MCDP 1 define friction?":
#     text alone ............ -2.42   (negative: judged irrelevant)
#     text with header ...... +3.39   (a 5.8-point swing)
# And it does not manufacture false positives -- asking about MCDP 6 still scores this
# MCDP 1 chunk at -6.16.
#
# The header is prepended for EMBEDDING and RERANKING only. Displayed text and quoted
# text stay clean, so nothing the user reads is polluted by retrieval scaffolding.

def retrieval_header(chunk):
    """Compact provenance line prepended to text for embedding and reranking."""
    bits = [chunk.get("pub_id") or ""]
    if chunk.get("pub_title") and chunk.get("pub_title") != chunk.get("pub_id"):
        bits.append(chunk["pub_title"])
    head = ", ".join(b for b in bits if b)

    parts = [head] if head else []
    ch_title = chunk.get("chapter_title")
    if ch_title:
        ch = chunk.get("chapter")
        parts.append(f"Chapter {ch}: {ch_title}" if ch else str(ch_title))
    if chunk.get("section"):
        parts.append(str(chunk["section"]))
    return " | ".join(parts)


def retrieval_text(chunk, body=None):
    """Header + body, as fed to the embedding model and the reranker."""
    text = body if body is not None else (chunk.get("text") or "")
    header = retrieval_header(chunk)
    return f"{header}\n{text}" if header else text
