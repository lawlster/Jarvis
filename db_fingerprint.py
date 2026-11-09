"""Step 57 — production-DB content fingerprint + Windows-safe temp cleanup.

Shared test helper (not a test module itself: the file name does
not match the ``test*.py`` discovery pattern, so the suite never
collects it).

Why this exists — two failures from the owner's Windows run on
2026-11-09 (Python 3.14, ``D:\\AiAssistant``,
``FAILED (failures=19, errors=96, skipped=2)``):

1.  The old guard hashed the production database's raw file
    bytes.  The owner's file hashes to ``355f80644af1...`` while
    this machine's hashes to ``36862922c0ba...`` — the same
    logical content, different bytes.  The bytes differ because
    of legitimate runtime writes (the last-20-messages memory
    rows, one ``provider_logs`` row from a live finance pull)
    and SQLite file-layout differences between machines.  File
    bytes are the wrong thing to guard.  :func:`content_fingerprint`
    hashes the logical content instead: every table except the
    volatile set, dumped in a deterministic order.  Identical
    content gives an identical fingerprint on any machine, and a
    real modification to a protected table changes it.
2.  Tests open SQLite connections to temp DB copies and never
    close them — ``with get_connection(...) as conn:`` commits
    but does not close, and the connection object survives until
    the cyclic garbage collector runs (verified by tracing live
    connections after ``agent.memory.save_message``: one leaked
    connection per ``with`` block, accumulating with every turn).
    On Linux deleting an open file succeeds, so nobody noticed;
    on Windows a file cannot be deleted while a connection to
    it is open, so ``TemporaryDirectory.cleanup()`` in tearDown
    failed with ``PermissionError: [WinError 32]``.  The product
    code still leaks those connections (a real fix for a later
    step — closing them changes no behaviour, but this step's
    scope is tests only); :func:`cleanup_temp_dir` forces a full
    GC pass before deleting, which closes them deterministically,
    and never lets a leftover temp dir fail the test.

Volatile tables — written by normal runtime use, so their
content is excluded from the fingerprint:

- ``conversations`` — the last-20-messages memory
  (``agent/memory.py``).
- ``provider_logs`` — one row per live finance pull
  (``data/providers/provider_logger.py``).
- ``sqlite_sequence`` — AUTOINCREMENT bookkeeping, which moves
  whenever a volatile table grows.

Every other table is protected by default (the fingerprint
covers all tables except the volatile set, so a table added
later is protected automatically): ``market_universe``,
``financial_statements``, ``financial_snapshots``,
``statement_collection_history``, ``valuation_profiles`` and
the legacy ``watchlist`` table.
"""

import gc
import hashlib
import shutil
import sqlite3
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PRODUCTION_DB = PROJECT_ROOT / "database" / "assistant.db"

# Tables whose content legitimately changes during normal runtime
# use.  Everything else in the database is protected.
VOLATILE_TABLES = frozenset(
    {"conversations", "provider_logs", "sqlite_sequence"})


def content_fingerprint(db_path):
    """Deterministic hash of a database's protected logical content.

    Opens the file read-only, dumps every table except the
    volatile set (table name, column names, row count, then every
    row sorted by its repr, so the order is stable regardless of
    storage order), and returns the SHA-256 hex digest.  Two files
    with identical protected content give the same fingerprint
    even when their raw bytes differ (different SQLite versions,
    VACUUM, header rewrites); changing one protected row changes
    it.
    """
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' ORDER BY name")]
        digest = hashlib.sha256()
        for table in tables:
            if table in VOLATILE_TABLES:
                continue
            columns = [row[1] for row in conn.execute(
                f'PRAGMA table_info("{table}")')]
            digest.update(f"table:{table}\n".encode("utf-8"))
            digest.update(
                f"columns:{','.join(columns)}\n".encode("utf-8"))
            rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            digest.update(f"rows:{len(rows)}\n".encode("utf-8"))
            for row in sorted(rows, key=repr):
                digest.update(repr(row).encode("utf-8"))
                digest.update(b"\n")
        return digest.hexdigest()
    finally:
        conn.close()


def production_fingerprint():
    """The content fingerprint of the project's production DB."""
    return content_fingerprint(PRODUCTION_DB)


def cleanup_temp_dir(tmp):
    """Delete a :class:`tempfile.TemporaryDirectory` that may hold
    a temp DB copy, without ever failing the calling test.

    Forces a full garbage-collection pass first: SQLite
    connections opened by ``with get_connection(...) as conn:``
    are never closed explicitly and survive until the cyclic
    collector runs, and on Windows a file cannot be deleted while
    a connection to it is open (WinError 32).  If the first
    attempt still fails, one more GC pass is tried; a leftover
    temp dir after that is removed best-effort and never raises.
    """
    gc.collect()
    try:
        tmp.cleanup()
    except (PermissionError, OSError):
        gc.collect()
        try:
            tmp.cleanup()
        except (PermissionError, OSError):
            shutil.rmtree(tmp.name, ignore_errors=True)


def temp_dir_with_db_cleanup():
    """A :class:`tempfile.TemporaryDirectory` safe to hold a DB copy
    on Windows: its exit-time finaliser never raises either."""
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
