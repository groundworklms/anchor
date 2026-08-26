"""SQLite concurrency for the shared records database.

The robustness audit (docs/ROBUSTNESS.md) found WAL mode held only because RecordStore
happened to be constructed before LearnStore -- a durability property depending on
construction order. These tests pin the behaviour LearnStore now sets for itself.
"""
import sqlite3
import sys
import threading

sys.path.insert(0, __import__("pathlib").Path(__file__).parent.joinpath("..", "src", "learn").resolve().as_posix())


def test_learnstore_sets_wal_and_busy_timeout_itself(tmp_path):
    """WAL and busy_timeout must not depend on RecordStore being built first."""
    from session import LearnStore
    items = tmp_path / "items.json"
    items.write_text('{"track": [], "items": []}', encoding="utf-8")
    db = tmp_path / "records.sqlite"
    # LearnStore FIRST -- the order the audit warned was unsafe.
    L = LearnStore(str(db), str(items))
    mode = L.db.execute("PRAGMA journal_mode").fetchone()[0]
    busy = L.db.execute("PRAGMA busy_timeout").fetchone()[0]
    assert mode.lower() == "wal", f"expected WAL, got {mode}"
    assert busy >= 5000, f"expected >=5000ms busy timeout, got {busy}"


def test_reader_proceeds_during_open_write(tmp_path):
    """WAL's whole point: a reader is not blocked by an uncommitted writer."""
    db = tmp_path / "c.sqlite"
    w = sqlite3.connect(db)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("CREATE TABLE t (x)")
    w.execute("INSERT INTO t VALUES (1)")
    w.commit()
    # Open a write transaction and leave it uncommitted.
    w.execute("BEGIN")
    w.execute("INSERT INTO t VALUES (2)")
    r = sqlite3.connect(db)
    r.execute("PRAGMA busy_timeout=2000")
    # The reader sees the committed row and is NOT blocked by the open write.
    assert r.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    w.commit()
    assert r.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_zero_busy_timeout_fails_fast_which_is_why_we_set_one(tmp_path):
    """Deterministic proof of WHY LearnStore sets a busy timeout: without one, a second
    writer against a held lock raises immediately. No threads -- two connections, one lock."""
    import pytest
    db = tmp_path / "b.sqlite"
    a = sqlite3.connect(db)
    a.execute("PRAGMA journal_mode=WAL")
    a.execute("CREATE TABLE t (x)")
    a.commit()
    a.execute("BEGIN IMMEDIATE")                 # a holds the write lock

    b = sqlite3.connect(db)
    b.execute("PRAGMA busy_timeout=0")           # no patience
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        b.execute("INSERT INTO t VALUES (1)")

    # The same operation on a connection configured the way LearnStore configures its own
    # would instead WAIT out the lock -- verified structurally by test 1, which asserts the
    # LearnStore connection carries busy_timeout >= 5000ms.
