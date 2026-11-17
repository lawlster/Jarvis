"""Step 61 regression tests: database connections must close.

A plain ``sqlite3.Connection`` only commits or rolls back on
``__exit__`` — it does NOT close.  Every ``with get_connection()
as conn:`` site therefore leaked one open connection (one open
file handle) for the life of the process.  Linux tolerates that;
Windows does not — the open handles block deleting or replacing
the database file, and a test run ends in a wall of WinError 32
PermissionErrors (the Step 57 failure on the owner's PC).

These tests run against temporary databases only, never the
production file, and prove the leak is gone:

* every ``get_connection`` closes its connection when the
  ``with`` block ends, on the happy path and on the error path;
* data written inside the block is still committed, and a failed
  block still rolls back;
* driving the fixed public paths repeatedly opens no new file
  descriptors for the database file;
* the database file can be deleted afterwards — the operation
  Windows refused while handles were open.

Unittest style, like the rest of the suite (``python -m unittest
discover``) — no pytest dependency.
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent import memory as memory_mod
from agents import alert_manager
from agents import financial_tracker
from agents import market_universe
from agents import scheduler as scheduler_mod
from agents import valuation_history
from agents import valuation_profiles
from agents import watchlist
from data.financial_statements import statement_database
from data.providers import provider_logger
from data.providers import provider_monitor

# (module, True when get_connection takes a db_path argument)
GET_CONNECTION_MODULES = [
    (memory_mod, True),
    (valuation_profiles, True),
    (provider_logger, True),
    (alert_manager, False),
    (scheduler_mod, False),
    (valuation_history, False),
    (watchlist, False),
    (market_universe, False),
    (statement_database, False),
    (provider_monitor, False),
]

# Modules whose get_connection reads a module-level DB_PATH.
MODULE_DB_MODULES = [m for m, takes_path in GET_CONNECTION_MODULES
                     if not takes_path]


def _fds_for(path):
    """Open file descriptors pointing at ``path`` (Linux only)."""
    fd_dir = "/proc/self/fd"
    if not os.path.isdir(fd_dir):
        return None
    target = os.path.realpath(str(path))
    count = 0
    for name in os.listdir(fd_dir):
        try:
            if os.path.realpath(os.path.join(fd_dir, name)) == target:
                count += 1
        except OSError:
            continue
    return count


class ConnectionLeakTest(unittest.TestCase):
    """Every get_connection closes on exit, happy path and error."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "leak.db")
        self._patched = []

    def _patch_db_path(self, module):
        """Point a module-level-DB_PATH module at the temp DB."""
        original = module.DB_PATH
        module.DB_PATH = Path(self.db_path)
        self.addCleanup(setattr, module, "DB_PATH", original)

    def _open_connection(self, module, takes_path):
        """Open a connection the way the module's own code does."""
        if takes_path:
            return module.get_connection(self.db_path)
        self._patch_db_path(module)
        return module.get_connection()

    def test_get_connection_closes_on_exit(self):
        for module, takes_path in GET_CONNECTION_MODULES:
            with self.subTest(module=module.__name__):
                with self._open_connection(module, takes_path) as conn:
                    held = conn
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS t (x INTEGER)")
                    conn.execute("INSERT INTO t VALUES (1)")
                with self.assertRaises(sqlite3.ProgrammingError):
                    held.execute("SELECT 1")

    def test_get_connection_closes_on_error(self):
        for module, takes_path in GET_CONNECTION_MODULES:
            with self.subTest(module=module.__name__):
                with self.assertRaises(RuntimeError):
                    with self._open_connection(module, takes_path) as conn:
                        held = conn
                        conn.execute(
                            "CREATE TABLE IF NOT EXISTS t (x INTEGER)")
                        raise RuntimeError("boom")
                with self.assertRaises(sqlite3.ProgrammingError):
                    held.execute("SELECT 1")

    def test_get_connection_still_commits(self):
        """Closing on exit must not lose the commit (or the rollback)."""
        with memory_mod.get_connection(self.db_path) as conn:
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (7)")
        check = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            self.assertEqual(
                check.execute("SELECT x FROM t").fetchone()[0], 7)
        finally:
            check.close()
        with self.assertRaises(RuntimeError):
            with memory_mod.get_connection(self.db_path) as conn:
                conn.execute("INSERT INTO t VALUES (8)")
                raise RuntimeError("boom")
        check = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)
        finally:
            check.close()

    def _drive_public_paths(self):
        """Exercise the fixed write/read paths once, on one temp DB."""
        for module in MODULE_DB_MODULES:
            self._patch_db_path(module)
        memory_mod.initialise_memory(self.db_path)
        memory_mod.save_message("user", "hello", self.db_path)
        memory_mod.load_recent_messages(db_path=self.db_path)
        valuation_profiles.initialise_valuation_profiles(self.db_path)
        valuation_profiles.save_valuation_profile(
            "MSFT", 0.10, 0.11, 0.025, db_path=self.db_path)
        valuation_profiles.get_valuation_profile(
            "MSFT", db_path=self.db_path)
        provider_logger.initialise_provider_logs(self.db_path)
        provider_logger.get_recent_provider_logs(db_path=self.db_path)
        alert_manager.initialise_alerts()
        alert_manager.create_alert("MSFT", "test", "leak check")
        alert_manager.get_recent_alerts()
        scheduler_mod.initialise_scheduler_history()
        scheduler_mod.get_monitoring_history()
        valuation_history.initialise_valuation_history()
        valuation_history.get_valuation_history("MSFT")
        watchlist.initialise_watchlist()
        watchlist.add_company("MSFT", "Microsoft")
        watchlist.get_watchlist()
        market_universe.initialise_market_universe()
        market_universe.get_market_universe_summary()
        statement_database.initialise_statement_database()
        statement_database.get_statement_database_summary()
        provider_monitor.get_provider_performance()
        financial_tracker.initialise_financial_tracker()
        financial_tracker.get_financial_history("MSFT")

    @unittest.skipIf(not os.path.isdir("/proc/self/fd"),
                     "file-descriptor counting needs /proc (Linux)")
    def test_public_paths_leak_no_descriptors(self):
        """Repeated use of the fixed paths opens no new descriptors
        for the database file — the leak, measured directly."""
        self.assertEqual(_fds_for(self.db_path), 0)
        for _ in range(3):
            self._drive_public_paths()
        self.assertEqual(_fds_for(self.db_path), 0)

    def test_database_file_deletable_after_use(self):
        """After the fixed paths run, the file can be deleted — the
        operation Windows refused while handles were open."""
        self._drive_public_paths()
        os.remove(self.db_path)
        self.assertFalse(os.path.exists(self.db_path))


if __name__ == "__main__":
    unittest.main()
