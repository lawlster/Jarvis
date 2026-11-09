"""Step 52 — backup, recovery and security systems: backup tests.

The backup module is exercised on temporary database copies
and temporary directories only.  The production database is
read, never written: its hash is asserted unchanged around
the whole class.  No network, no production writes.
"""

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents import backup_recovery as br  # noqa: E402

EXPECTED_DB_HASH = (
    "36862922c0babfb2569369d297bbd1a91579573bd51133b1f9b8aa56cc089187")


def _db_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _make_db(path, rows=((1, "alpha"), (2, "beta"))):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, "
                     "name TEXT)")
        conn.executemany("INSERT INTO items VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()
    return path


class BackupRecoveryTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.production_hash_before = _db_hash(
            PROJECT_ROOT / "database" / "assistant.db")

    @classmethod
    def tearDownClass(cls):
        after = _db_hash(PROJECT_ROOT / "database" / "assistant.db")
        assert after == cls.production_hash_before, (
            "production DB changed during the backup tests")
        assert after == EXPECTED_DB_HASH

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "assistant.db"
        shutil.copy(PROJECT_ROOT / "database" / "assistant.db",
                    self.db_path)
        self.backup_dir = self.root / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    # -- backup -----------------------------------------------------

    def test_backup_real_database_copy(self):
        record = br.backup_database(
            self.db_path, backup_dir=self.backup_dir,
            now="20261109T000000Z")
        backup_file = self.backup_dir / record["backup_file"]
        self.assertTrue(backup_file.exists())
        self.assertEqual(record["sha256"],
                         br.sha256_file(backup_file))
        self.assertEqual(record["integrity"], "ok")
        self.assertEqual(record["source"], str(self.db_path))
        # The source copy is untouched by the backup itself.
        self.assertEqual(_db_hash(self.db_path),
                         _db_hash(PROJECT_ROOT / "database"
                                  / "assistant.db"))
        manifest = (self.backup_dir / "backups.jsonl").read_text(
            encoding="utf-8")
        self.assertIn(record["backup_file"], manifest)
        self.assertIn(record["sha256"], manifest)

    def test_backup_missing_source(self):
        with self.assertRaises(FileNotFoundError):
            br.backup_database(self.root / "no-such.db",
                               backup_dir=self.backup_dir)

    def test_backup_refuses_corrupt_source(self):
        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(b"this is not a sqlite database")
        with self.assertRaises(ValueError):
            br.backup_database(corrupt, backup_dir=self.backup_dir)
        self.assertFalse(list(self.backup_dir.glob("*.bak"))
                         if self.backup_dir.exists() else [])

    def test_backup_label_in_filename(self):
        record = br.backup_database(
            self.db_path, backup_dir=self.backup_dir,
            label="pre-15J drill", now="20261109T000000Z")
        self.assertIn("pre-15J-drill", record["backup_file"])

    # -- verify -----------------------------------------------------

    def test_verify_backup_ok(self):
        record = br.backup_database(
            self.db_path, backup_dir=self.backup_dir,
            now="20261109T000000Z")
        result = br.verify_backup(
            self.backup_dir / record["backup_file"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["sha256_matches"])
        self.assertEqual(result["integrity"], "ok")

    def test_verify_backup_detects_tampering(self):
        record = br.backup_database(
            self.db_path, backup_dir=self.backup_dir,
            now="20261109T000000Z")
        backup_file = self.backup_dir / record["backup_file"]
        with open(backup_file, "r+b") as handle:
            handle.seek(100)
            handle.write(b"\x00\x00\x00\x00")
        result = br.verify_backup(backup_file)
        self.assertFalse(result["ok"])
        self.assertFalse(result["sha256_matches"])

    def test_verify_backup_missing_file(self):
        result = br.verify_backup(self.backup_dir / "nope.bak")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["reason"])

    # -- restore + recovery drill -----------------------------------

    def test_recovery_drill(self):
        """Back up, corrupt the copy, restore, hashes match."""
        record = br.backup_database(
            self.db_path, backup_dir=self.backup_dir,
            now="20261109T000000Z")
        original_hash = _db_hash(self.db_path)
        backup_file = self.backup_dir / record["backup_file"]
        # Corrupt the working copy: drop a table.
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DROP TABLE market_universe")
            conn.commit()
        finally:
            conn.close()
        self.assertNotEqual(_db_hash(self.db_path), original_hash)
        restored = br.restore_backup(
            backup_file, self.db_path,
            production_db_path=self.root / "production.db")
        # The restored copy is byte-identical to the backup that
        # was verified, and its logical content is whole again.
        self.assertEqual(_db_hash(self.db_path),
                         _db_hash(backup_file))
        self.assertEqual(restored["sha256"], record["sha256"])
        ok, _ = br._integrity_check(self.db_path)
        self.assertTrue(ok)
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM market_universe").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 10619)

    def test_restore_refuses_production_target(self):
        record = br.backup_database(
            self.db_path, backup_dir=self.backup_dir,
            now="20261109T000000Z")
        backup_file = self.backup_dir / record["backup_file"]
        production = self.root / "production.db"
        shutil.copy(self.db_path, production)
        with self.assertRaises(ValueError):
            br.restore_backup(backup_file, production,
                              production_db_path=production)
        # The production stand-in is untouched.
        self.assertEqual(_db_hash(production), _db_hash(self.db_path))

    def test_restore_refuses_unverified_backup(self):
        record = br.backup_database(
            self.db_path, backup_dir=self.backup_dir,
            now="20261109T000000Z")
        backup_file = self.backup_dir / record["backup_file"]
        with open(backup_file, "r+b") as handle:
            handle.seek(100)
            handle.write(b"\x00\x00\x00\x00")
        with self.assertRaises(ValueError):
            br.restore_backup(backup_file, self.root / "restored.db",
                              production_db_path=self.root
                              / "production.db")
        self.assertFalse((self.root / "restored.db").exists())

    def test_restore_refuses_self_target(self):
        record = br.backup_database(
            self.db_path, backup_dir=self.backup_dir,
            now="20261109T000000Z")
        backup_file = self.backup_dir / record["backup_file"]
        with self.assertRaises(ValueError):
            br.restore_backup(backup_file, backup_file,
                              production_db_path=self.root
                              / "production.db")

    # -- list / prune -----------------------------------------------

    def test_list_backups_newest_first(self):
        first = br.backup_database(
            self.db_path, backup_dir=self.backup_dir,
            now="20261109T000000Z")
        second = br.backup_database(
            self.db_path, backup_dir=self.backup_dir,
            now="20261109T010000Z")
        listing = br.list_backups(self.backup_dir)
        names = [b["backup_file"] for b in listing["backups"]]
        self.assertEqual(names, [second["backup_file"],
                                 first["backup_file"]])
        self.assertEqual(listing["skipped_lines"], 0)

    def test_list_backups_skips_corrupt_manifest_lines(self):
        br.backup_database(self.db_path, backup_dir=self.backup_dir,
                           now="20261109T000000Z")
        manifest = self.backup_dir / "backups.jsonl"
        with open(manifest, "a", encoding="utf-8") as handle:
            handle.write("this is not json\n")
            handle.write('{"no_backup_file": true}\n')
        listing = br.list_backups(self.backup_dir)
        self.assertEqual(len(listing["backups"]), 1)
        self.assertEqual(listing["skipped_lines"], 2)

    def test_prune_keeps_newest(self):
        names = []
        for stamp in ("20261109T000000Z", "20261109T010000Z",
                      "20261109T020000Z"):
            record = br.backup_database(
                self.db_path, backup_dir=self.backup_dir, now=stamp)
            names.append(record["backup_file"])
        result = br.prune_backups(self.backup_dir, keep=2)
        self.assertEqual(result["kept"], [names[2], names[1]])
        self.assertEqual(result["deleted"], [names[0]])
        self.assertFalse((self.backup_dir / names[0]).exists())
        self.assertTrue((self.backup_dir / names[2]).exists())
        listing = br.list_backups(self.backup_dir)
        self.assertEqual(len(listing["backups"]), 2)

    def test_prune_keep_below_one_refused(self):
        br.backup_database(self.db_path, backup_dir=self.backup_dir,
                           now="20261109T000000Z")
        with self.assertRaises(ValueError):
            br.prune_backups(self.backup_dir, keep=0)
        self.assertEqual(len(list(self.backup_dir.glob("*.bak"))), 1)

    # -- data-store snapshots ---------------------------------------

    def _seed_data_dir(self):
        data_dir = self.root / "data"
        (data_dir / "watchlist").mkdir(parents=True)
        (data_dir / "watchlist" / "watchlist.json").write_text(
            json.dumps({"tickers": ["MSFT"]}), encoding="utf-8")
        (data_dir / "memory").mkdir()
        (data_dir / "memory" / "index.json").write_text(
            json.dumps({"records": 1}), encoding="utf-8")
        (data_dir / "events").mkdir()
        (data_dir / "events" / "evt.json").write_text(
            "{}", encoding="utf-8")
        return data_dir

    def test_data_store_snapshot_and_verify(self):
        data_dir = self._seed_data_dir()
        manifest = br.backup_data_stores(
            data_dir, backup_dir=self.backup_dir,
            now="20261109T000000Z")
        self.assertEqual(manifest["file_count"], 3)
        self.assertIn("theses", manifest["skipped_stores"])
        snapshot_dir = self.backup_dir / manifest["snapshot"]
        self.assertTrue((snapshot_dir / "manifest.json").exists())
        self.assertTrue(
            (snapshot_dir / "watchlist" / "watchlist.json").exists())
        result = br.verify_data_backup(snapshot_dir)
        self.assertTrue(result["ok"])
        self.assertEqual(result["checked"], 3)

    def test_data_store_verify_detects_change_and_loss(self):
        data_dir = self._seed_data_dir()
        manifest = br.backup_data_stores(
            data_dir, backup_dir=self.backup_dir,
            now="20261109T000000Z")
        snapshot_dir = self.backup_dir / manifest["snapshot"]
        (snapshot_dir / "memory" / "index.json").write_text(
            json.dumps({"records": 999}), encoding="utf-8")
        (snapshot_dir / "events" / "evt.json").unlink()
        result = br.verify_data_backup(snapshot_dir)
        self.assertFalse(result["ok"])
        self.assertEqual(result["mismatched"],
                         ["memory/index.json"])
        self.assertEqual(result["missing"], ["events/evt.json"])

    def test_data_store_verify_missing_manifest(self):
        empty = self.root / "empty-snapshot"
        empty.mkdir()
        result = br.verify_data_backup(empty)
        self.assertFalse(result["ok"])
        self.assertIn("manifest", result["reason"])


if __name__ == "__main__":
    unittest.main()
