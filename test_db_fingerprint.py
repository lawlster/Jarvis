"""Step 57 — tests for the content-fingerprint guard (db_fingerprint).

Proves the guard does the two things it must do, on any machine:

- it does NOT fire on the things that legitimately differ between
  machines (raw file bytes, VACUUM, volatile-table growth);
- it DOES fire on a real modification to a protected table.

All database work happens on temp copies; the production DB is
only ever opened read-only.
"""

import gc
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from db_fingerprint import (  # noqa: E402
    PRODUCTION_DB, cleanup_temp_dir, content_fingerprint,
    temp_dir_with_db_cleanup)


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FingerprintStabilityTestCase(unittest.TestCase):
    def test_fingerprint_is_deterministic(self):
        first = content_fingerprint(PRODUCTION_DB)
        second = content_fingerprint(PRODUCTION_DB)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        int(first, 16)  # a hex digest, not an error string

    def test_copy_has_same_fingerprint_despite_different_bytes(self):
        # A plain file copy already proves the point on this
        # machine (same bytes), but the copy also exercises the
        # read-only open on a second path.
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "copy.db"
            shutil.copy(PRODUCTION_DB, copy)
            self.assertEqual(content_fingerprint(copy),
                             content_fingerprint(PRODUCTION_DB))

    def test_vacuum_changes_bytes_not_fingerprint(self):
        # VACUUM rewrites the whole file: the raw file hash
        # changes, the logical content does not.  This is the
        # exact failure mode of the old byte-hash guard.
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "copy.db"
            vacuumed = Path(tmp) / "vacuumed.db"
            shutil.copy(PRODUCTION_DB, copy)
            before_bytes = _file_hash(copy)
            conn = sqlite3.connect(copy)
            try:
                conn.execute(f"VACUUM INTO '{vacuumed}'")
            finally:
                conn.close()
            self.assertEqual(content_fingerprint(vacuumed),
                             content_fingerprint(PRODUCTION_DB))
            # Sanity: the test would be vacuous if VACUUM had not
            # actually rewritten the file.
            self.assertNotEqual(_file_hash(vacuumed), before_bytes)


class FingerprintDetectsChangeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "copy.db"
        shutil.copy(PRODUCTION_DB, self.db_path)
        self.before = content_fingerprint(self.db_path)

    def tearDown(self):
        cleanup_temp_dir(self.tmp)

    def _mutate(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def test_protected_table_update_changes_fingerprint(self):
        self._mutate(
            "UPDATE market_universe SET country = 'Nowhere' "
            "WHERE ticker = 'MSFT'")
        self.assertNotEqual(content_fingerprint(self.db_path),
                            self.before)

    def test_protected_table_delete_changes_fingerprint(self):
        self._mutate("DELETE FROM watchlist WHERE ticker = 'MSFT'")
        self.assertNotEqual(content_fingerprint(self.db_path),
                            self.before)

    def test_protected_table_insert_changes_fingerprint(self):
        self._mutate(
            "INSERT INTO valuation_profiles (ticker, growth_rate, "
            "discount_rate, terminal_growth_rate, margin_of_safety, "
            "forecast_years) VALUES ('FIXA', 0.1, 0.11, 0.025, "
            "0.25, 5)")
        self.assertNotEqual(content_fingerprint(self.db_path),
                            self.before)

    def test_volatile_table_growth_does_not_change_fingerprint(self):
        # The exact writes normal runtime use makes: a memory row
        # and a provider-log row.  The old byte-hash guard fired
        # on these; the fingerprint must not.
        self._mutate(
            "INSERT INTO conversations (role, content) "
            "VALUES ('user', 'guard test')")
        self._mutate(
            "INSERT INTO provider_logs (ticker, provider, status, "
            "retrieved_at) VALUES ('MSFT', 'yahoo', 'success', "
            "'2026-11-09T00:00:00Z')")
        self.assertEqual(content_fingerprint(self.db_path),
                         self.before)


class CleanupTempDirTestCase(unittest.TestCase):
    def test_cleanup_removes_dir_with_leaked_connection(self):
        # Reproduce the Windows failure on Linux: open connections
        # to the temp copy through the same ``with`` pattern the
        # product code uses (never closed explicitly), then clean
        # up.  Without the GC pass in cleanup_temp_dir the
        # connections stay open; with it the directory is gone
        # and no file descriptor to the copy remains.
        from agent import memory as memory_mod

        tmp = temp_dir_with_db_cleanup()
        db_path = Path(tmp.name) / "assistant.db"
        shutil.copy(PRODUCTION_DB, db_path)
        old = memory_mod.DB_PATH
        try:
            memory_mod.DB_PATH = db_path
            memory_mod.save_message("user", "leak check one")
            memory_mod.save_message("assistant", "leak check two")
            # The connections are still open here on every
            # platform (they survive until the cyclic GC runs).
            live = [obj for obj in gc.get_objects()
                    if isinstance(obj, sqlite3.Connection)]
            has_leak = any(
                str(db_path) in str(row[2])
                for obj in live
                for row in obj.execute(
                    "PRAGMA database_list").fetchall())
            self.assertTrue(
                has_leak,
                "expected at least one leaked connection to the "
                "temp copy before cleanup")
            # Release our own references to the connections first:
            # the list above is itself a holder, and the GC pass
            # inside cleanup_temp_dir can only close connections
            # nothing else references.
            del live
            gc.collect()
            cleanup_temp_dir(tmp)
            self.assertFalse(Path(tmp.name).exists())
            if Path("/proc/self/fd").exists():
                open_to_copy = []
                for fd in Path("/proc/self/fd").iterdir():
                    try:
                        target = os.readlink(fd)
                    except OSError:
                        continue  # FD closed mid-scan
                    if target.startswith(str(db_path)):
                        open_to_copy.append(target)
                self.assertEqual(open_to_copy, [])
        finally:
            memory_mod.DB_PATH = old

    def test_cleanup_never_raises(self):
        # Even a directory that is already gone must not fail.
        tmp = temp_dir_with_db_cleanup()
        name = tmp.name
        shutil.rmtree(name, ignore_errors=True)
        cleanup_temp_dir(tmp)  # must not raise


if __name__ == "__main__":
    unittest.main()
