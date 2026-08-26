"""Path and import setup for the unit suite.

The source tree is not a package. `scripts/push.sh` scp's each directory's *.py flat
into its own directory on the Jetson, so a module written as `from review import
ReviewStore` resolves because review.py sits beside session.py once deployed. Importing
them here therefore means reproducing the DEPLOYED layout, not the repo layout:

  * src/learn      -- session.py does `from review import ReviewStore`
  * src/common     -- push.sh copies chunk_text.py into retrieval/ and ingest/, so
                      `from chunk_text import retrieval_text` is a sibling import there.
                      Putting src/common on the path is the repo-side equivalent; if the
                      two ever diverged the tests would import a different chunk_text
                      than the device runs, which is exactly the drift chunk_text.py's
                      own docstring exists to prevent.

Everything in this suite is pure logic: no model server, no sockets, no device, no GPU.
If a test here ever needs one of those, it belongs in scripts/boot_test.py instead.
"""
import sys
import types
from pathlib import Path

# Importing source modules from src/* would otherwise drop a __pycache__ directory into
# each one. The suite must leave the tree exactly as it found it -- these directories are
# deployed to the device verbatim by push.sh, and stale bytecode from a workstation is
# not something that should ever ride along.
sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

# Deployed-layout import roots, in the order push.sh lays them out.
for d in ("learn", "ingest", "api", "common", "retrieval", "records", "generation"):
    p = str(SRC / d)
    if p not in sys.path:
        sys.path.insert(0, p)


# sqlite-vec is a device dependency: it is installed on the Jetson and imported at the
# top of hybrid.py, but it is only ever USED inside connect(), which opens the real
# index. The functions under test here (fts_query, rrf_fuse) touch none of that. Rather
# than skip the whole module on a workstation that has no vector extension, stand in a
# stub so the import succeeds. The stub deliberately raises if anything actually calls
# it, so a test that quietly started needing a real index would fail loudly instead of
# passing against a no-op.
if "sqlite_vec" not in sys.modules:
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        stub = types.ModuleType("sqlite_vec")

        def _load(_db):
            raise RuntimeError(
                "sqlite_vec is stubbed in the unit suite -- this test tried to open a "
                "real vector index, which is not a unit test")

        stub.load = _load
        sys.modules["sqlite_vec"] = stub
