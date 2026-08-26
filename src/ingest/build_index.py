#!/usr/bin/env python3
"""Build the single-file SQLite index: sqlite-vec (dense) + FTS5 (BM25).

Runs ON the device, because embedding 4,600 chunks wants the GPU. Produces one portable
artifact -- copy `doctrine.sqlite` and the whole retrieval layer moves with it.

Embeddings come from llama.cpp's OpenAI-compatible /v1/embeddings endpoint rather than a
Python model runtime. That keeps a single inference engine on the box (no PyTorch on an
8GB shared-memory board) and keeps the model swappable by config, per the brief.

    python3 build_index.py --chunks chunks.jsonl --db doctrine.sqlite
"""
import argparse
import json
import os
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request

import sqlite_vec

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from chunk_text import retrieval_text  # noqa: E402  (shared with the retrieval path)

EMBED_DIM = 384  # bge-small-en-v1.5


def _post_embed(base_url, texts, timeout=300):
    payload = json.dumps({"input": texts, "model": "bge-small-en-v1.5"}).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/embeddings",
        data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    rows = sorted(data["data"], key=lambda d: d.get("index", 0))
    return [row["embedding"] for row in rows]


TRUNCATIONS = []


def embed_batch(base_url, texts, retries=2):
    """POST to /v1/embeddings, degrading gracefully rather than aborting the build.

    The server rejects the whole request if any single input exceeds the model's 512-token
    limit, so a batch failure says nothing about which item was at fault. On failure the
    batch is bisected to isolate the offender, and only that item is shortened -- with the
    truncation recorded, because silently shortening text is exactly the kind of quiet
    loss this project should not tolerate.
    """
    last = None
    for attempt in range(retries):
        try:
            return _post_embed(base_url, texts)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, KeyError) as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code in (400, 500):
                break                      # size problem: retrying identically won't help
            time.sleep(2 * (attempt + 1))

    if len(texts) == 1:
        t = texts[0]
        for limit in (900, 700, 500, 350):
            try:
                vec = _post_embed(base_url, [t[:limit]])
                TRUNCATIONS.append({"orig_chars": len(t), "used_chars": limit})
                return vec
            except Exception:                             # noqa: BLE001
                continue
        raise RuntimeError(f"could not embed a single unit ({len(t)} chars): {last}")

    mid = len(texts) // 2
    return embed_batch(base_url, texts[:mid]) + embed_batch(base_url, texts[mid:])


def pack(vec):
    return struct.pack(f"{len(vec)}f", *vec)


# bge-small-en-v1.5 accepts 512 tokens. Chunks run to 10,279 characters (~2,500 tokens),
# so 2.4% of them would be silently truncated -- their tails absent from the dense vector
# and therefore unfindable by semantic search, while still looking indexed.
#
# Long chunks are embedded as overlapping windows instead, with one vector per window all
# pointing at the same chunk. Retrieval takes a chunk's best-scoring window. Truncation
# would have been simpler and quietly lossy; this keeps every sentence reachable.
# Sized in characters against a hard 512-token model limit. Doctrinal prose here runs
# about 2.8 characters per token (measured: a 1,500-char window produced 540 tokens and
# was rejected), so 1,000 characters leaves real headroom rather than a theoretical one.
WINDOW_CHARS = 1000
WINDOW_OVERLAP = 200
WINDOW_TRIGGER = 1150


def windows(text):
    if len(text) <= WINDOW_TRIGGER:
        return [text]
    out, start = [], 0
    step = WINDOW_CHARS - WINDOW_OVERLAP
    while start < len(text):
        out.append(text[start:start + WINDOW_CHARS])
        if start + WINDOW_CHARS >= len(text):
            break
        start += step
    return out


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY,
    pub_id          TEXT NOT NULL,
    pub_title       TEXT,
    edition         TEXT,
    chapter         INTEGER,
    chapter_title   TEXT,
    section         TEXT,
    section_conf    REAL,
    para_id         TEXT,
    page_printed    TEXT,
    page_pdf_start  INTEGER,
    page_pdf_end    INTEGER,
    spans_pages     INTEGER,
    citation        TEXT NOT NULL,
    text            TEXT NOT NULL,
    n_chars         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_chunks_pub ON chunks(pub_id);

-- BM25 half of the hybrid, keyed by rowid = chunks.id so both retrieval paths return
-- the same ids and fuse cleanly.
--
-- Standalone rather than external-content, because what gets indexed is header+text, not
-- the raw body: "MCDP 1" appears only in metadata, so a lexical search for it would
-- otherwise miss every chunk of MCDP 1. The dense side has the same problem and the same
-- fix; keeping both on identical text is what makes the fusion meaningful.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    tokenize='porter unicode61'
);

-- Provenance travels inside the artifact. A copied index that cannot say where it came
-- from is not auditable, and this project is judged on trust.
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--embed-url", default="http://127.0.0.1:8081")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--manifest")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.chunks, encoding="utf-8") if l.strip()]
    print(f"loaded {len(rows):,} chunks")

    db = sqlite3.connect(args.db)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript(SCHEMA)
    db.execute("DROP TABLE IF EXISTS chunks_vec")
    db.execute(f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{EMBED_DIM}])")
    db.execute("DROP TABLE IF EXISTS vec_map")
    db.execute("CREATE TABLE vec_map (vec_rowid INTEGER PRIMARY KEY, "
               "chunk_id INTEGER NOT NULL, window_idx INTEGER NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_vecmap_chunk ON vec_map(chunk_id)")
    db.execute("DELETE FROM chunks")
    db.execute("DELETE FROM chunks_fts")

    for i, c in enumerate(rows, 1):
        db.execute(
            "INSERT INTO chunks (id,pub_id,pub_title,edition,chapter,chapter_title,"
            "section,section_conf,para_id,page_printed,page_pdf_start,page_pdf_end,"
            "spans_pages,citation,text,n_chars) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, c["pub_id"], c.get("pub_title"), c.get("edition"), c.get("chapter"),
             c.get("chapter_title"), c.get("section"), c.get("section_match_confidence"),
             c.get("para_id"), str(c["page_printed"]) if c.get("page_printed") else None,
             c.get("page_pdf_start"), c.get("page_pdf_end"),
             1 if c.get("spans_pages") else 0, c["citation"], c["text"],
             c.get("n_chars", len(c["text"]))))
    db.commit()
    for i, c in enumerate(rows, 1):
        db.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?,?)",
                   (i, retrieval_text(c)))
    db.commit()
    print(f"inserted {len(rows):,} rows; FTS5 built")

    # Flatten to (chunk_id, window_idx, text) so batching is uniform regardless of how
    # many windows any one chunk produced.
    # Each window is embedded WITH its provenance header, so a query naming the
    # publication can match. See src/common/chunk_text.py for the measurement.
    units = []
    for cid, c in enumerate(rows, 1):
        for wi, w in enumerate(windows(c["text"])):
            units.append((cid, wi, retrieval_text(c, w)))
    extra = len(units) - len(rows)
    print(f"embedding {len(units):,} windows for {len(rows):,} chunks "
          f"({extra:,} extra from long-chunk windowing)")

    t0 = time.time()
    vec_rowid = 0
    for start in range(0, len(units), args.batch):
        batch = units[start:start + args.batch]
        vecs = embed_batch(args.embed_url, [b[2] for b in batch])
        if len(vecs) != len(batch):
            sys.exit(f"embedding count mismatch: {len(vecs)} vs {len(batch)}")
        for (cid, wi, _), v in zip(batch, vecs):
            if len(v) != EMBED_DIM:
                sys.exit(f"unexpected embedding dim {len(v)} (want {EMBED_DIM})")
            vec_rowid += 1
            db.execute("INSERT INTO chunks_vec(rowid, embedding) VALUES (?,?)",
                       (vec_rowid, pack(v)))
            db.execute("INSERT INTO vec_map(vec_rowid, chunk_id, window_idx) "
                       "VALUES (?,?,?)", (vec_rowid, cid, wi))
        done = start + len(batch)
        if done % (args.batch * 20) == 0 or done == len(units):
            rate = done / max(1e-9, time.time() - t0)
            eta = (len(units) - done) / max(1e-9, rate)
            print(f"  embedded {done:,}/{len(units):,}  ({rate:.1f}/s, eta {eta:.0f}s)")
        db.commit()

    meta = {
        "embedding_model": "bge-small-en-v1.5-q8_0.gguf",
        "embedding_dim": str(EMBED_DIM),
        "chunk_count": str(len(rows)),
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "built_note": "device RTC is unreliable; see DECISIONS.md D-010",
    }
    if args.manifest:
        m = json.loads(open(args.manifest, encoding="utf-8").read())
        meta["corpus_documents"] = str(m["totals"]["documents"])
        meta["corpus_pages"] = str(m["totals"]["pages"])
        meta["corpus_shas"] = json.dumps(
            {d["pub_id"]: d["sha256"][:16] for d in m["documents"]})
    for k, v in meta.items():
        db.execute("INSERT OR REPLACE INTO index_meta(key,value) VALUES (?,?)", (k, v))
    db.commit()

    db.execute("PRAGMA optimize")
    db.execute("VACUUM")

    # Ship as ONE file. The brief calls for a single portable artifact, and WAL mode
    # leaves doctrine.sqlite-wal and -shm beside it -- copy only the .sqlite and you
    # silently lose whatever is still in the write-ahead log, and its checksum means
    # nothing. Checkpoint the WAL back into the main file and switch to a rollback
    # journal: the index is read-only in service, so WAL's concurrency buys us nothing.
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("PRAGMA journal_mode=DELETE")
    db.commit()
    db.close()

    for sidecar in (args.db + "-wal", args.db + "-shm"):
        if os.path.exists(sidecar):
            os.remove(sidecar)

    print(f"\nwrote {args.db}  ({os.path.getsize(args.db)/1048576:.1f} MB)")
    print(f"embedding took {time.time()-t0:.0f}s")
    if TRUNCATIONS:
        print(f"! {len(TRUNCATIONS)} window(s) were shortened to fit the 512-token "
              f"embedding limit:")
        for t in TRUNCATIONS[:10]:
            print(f"    {t['orig_chars']} chars -> {t['used_chars']}")
        print("  Their full text is still indexed for BM25 and still returned verbatim; "
              "only the dense vector saw less.")


if __name__ == "__main__":
    main()
