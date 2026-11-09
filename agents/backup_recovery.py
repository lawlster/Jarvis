"""Step 52 — backup, recovery and security systems (backup half).

Database backups and JSON data-store snapshots, plus a recovery
drill path.  Rules the module enforces:

- A backup is a consistent snapshot: the database copy is made
  through SQLite's backup API from a read-only source
  connection, and the source must pass ``integrity_check``
  first — a corrupt database is never backed up silently.
- Every backup is recorded in a JSON-lines manifest
  (``backups.jsonl``) with its SHA-256 hash, size, timestamp
  and source, so a backup can be verified later without
  trusting its filename.
- Restore is always to a named target file and always verifies
  the backup (hash + integrity) first.  Restore REFUSES to
  overwrite the production database: replacing the live
  database is an explicit, owner-approved step this module
  does not perform.  Restore may overwrite any other existing
  target — that is the point of a recovery drill.
- Pruning keeps the N most recent backups and never deletes
  the newest one; ``keep`` below 1 is refused.
- Data-store snapshots copy the JSON stores (watchlist,
  theses, events, news, scheduler, alerts, memory, monitoring,
  macro, reports, research sessions, web research,
  orchestration) with a per-file hash manifest, so a snapshot
  can be verified file by file.

Nothing here sends anything anywhere, and nothing here writes
to the production database.  All paths are caller-supplied in
tests; the default backup directory is the project's existing
``jarvis-backups`` directory next to the repo (where the
Phase A import backups already live), overridable with the
``JARVIS_BACKUP_DIR`` environment variable.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_DB_PATH = PROJECT_ROOT / "database" / "assistant.db"
DEFAULT_BACKUP_DIR = Path(
    os.environ.get("JARVIS_BACKUP_DIR") or
    (PROJECT_ROOT.parent / "jarvis-backups"))
MANIFEST_NAME = "backups.jsonl"

# JSON data stores that belong in a data-store snapshot.  Only
# directories that exist are copied; missing ones are listed as
# skipped, never an error (most stores start empty).
DATA_STORE_DIRS = (
    "watchlist",
    "theses",
    "events",
    "news",
    "scheduler",
    "alerts",
    "memory",
    "monitoring",
    "macro",
    "quarterly_reports",
    "research_sessions",
    "web_research",
    "orchestration",
)


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path):
    """SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity_check(db_path):
    """``PRAGMA integrity_check`` on a read-only connection.

    Returns (ok, detail): ok is True only when the check
    returns exactly ``ok``.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    detail = "; ".join(str(row[0]) for row in rows)
    return (len(rows) == 1 and rows[0][0] == "ok"), detail


def _manifest_path(backup_dir):
    return Path(backup_dir) / MANIFEST_NAME


def _append_manifest(backup_dir, record):
    path = _manifest_path(backup_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def _read_manifest(backup_dir):
    """Manifest records plus how many lines were corrupt.

    Corrupt lines are skipped and counted, never a crash —
    the same convention as the other JSON-lines logs.
    """
    path = _manifest_path(backup_dir)
    records = []
    skipped = 0
    if not path.exists():
        return records, skipped
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(record, dict) or "backup_file" not in record:
            skipped += 1
            continue
        records.append(record)
    return records, skipped


def _rewrite_manifest(backup_dir, records):
    path = _manifest_path(backup_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Database backup / verify / restore / list / prune
# ---------------------------------------------------------------------------

def backup_database(db_path, backup_dir=None, label=None, now=None):
    """Back up one SQLite database.

    Returns the manifest record.  Raises FileNotFoundError
    when the source is missing and ValueError when the source
    fails integrity_check (a corrupt database is not backed
    up).  The production database is only ever read, never
    written, by this function.
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir) if backup_dir else DEFAULT_BACKUP_DIR
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    try:
        ok, detail = _integrity_check(db_path)
    except sqlite3.Error as exc:
        raise ValueError(
            "refusing to back up a database that cannot be read: "
            f"{exc}") from exc
    if not ok:
        raise ValueError(
            f"refusing to back up a database that fails "
            f"integrity_check: {detail}")

    stamp = now or _timestamp()
    stem = "assistant.db"
    if label:
        safe_label = "".join(
            ch if (ch.isalnum() or ch in "-_") else "-"
            for ch in str(label)).strip("-") or "backup"
        name = f"{stem}.{safe_label}.{stamp}.bak"
    else:
        name = f"{stem}.{stamp}.bak"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / name

    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    record = {
        "backup_file": name,
        "sha256": sha256_file(target),
        "size_bytes": target.stat().st_size,
        "created_at": _utcnow_iso(),
        "source": str(db_path),
        "label": label,
        "integrity": "ok",
    }
    _append_manifest(backup_dir, record)
    return record


def verify_backup(backup_path, expected_sha256=None):
    """Verify a database backup: hash match + integrity_check.

    Returns a dict with ``ok`` True only when the file exists,
    the hash matches (when an expected hash is supplied, or
    when one is recorded in the manifest next to the file),
    and integrity_check passes.  Never raises for a bad
    backup — a bad backup is a result, not a crash.
    """
    backup_path = Path(backup_path)
    result = {"path": str(backup_path), "ok": False,
              "sha256": None, "sha256_matches": None,
              "integrity": None, "reason": None}
    if not backup_path.exists():
        result["reason"] = "backup file not found"
        return result
    result["sha256"] = sha256_file(backup_path)
    if expected_sha256 is None:
        manifest = _manifest_path(backup_path.parent)
        if manifest.exists():
            records, _ = _read_manifest(backup_path.parent)
            for record in records:
                if record.get("backup_file") == backup_path.name:
                    expected_sha256 = record.get("sha256")
                    break
    if expected_sha256 is not None:
        result["sha256_matches"] = (
            result["sha256"] == expected_sha256)
        if not result["sha256_matches"]:
            result["reason"] = "SHA-256 does not match the manifest"
            return result
    try:
        ok, detail = _integrity_check(backup_path)
    except sqlite3.Error as exc:
        result["reason"] = f"integrity_check failed to run: {exc}"
        return result
    result["integrity"] = "ok" if ok else detail
    if not ok:
        result["reason"] = f"integrity_check failed: {detail}"
        return result
    result["ok"] = True
    result["reason"] = "hash matches; integrity_check ok"
    return result


def restore_backup(backup_path, target_path, production_db_path=None):
    """Restore a backup to a named target file.

    Verifies the backup first (hash + integrity).  REFUSES
    when the target resolves to the production database —
    replacing the live database is an explicit, owner-approved
    step this module does not perform — and when the target is
    the backup file itself.  Overwriting any other existing
    target is allowed: that is what a recovery drill does.
    Returns a record for the restored copy.
    """
    backup_path = Path(backup_path)
    target_path = Path(target_path)
    production = Path(production_db_path) if production_db_path \
        else PRODUCTION_DB_PATH
    if target_path.resolve() == production.resolve():
        raise ValueError(
            "refusing to restore over the production database "
            f"({production}); restore to a named target file and "
            "replace the live database only by an explicit, "
            "owner-approved step")
    if target_path.resolve() == backup_path.resolve():
        raise ValueError(
            "refusing to restore a backup onto itself")
    verification = verify_backup(backup_path)
    if not verification["ok"]:
        raise ValueError(
            "refusing to restore a backup that does not verify: "
            f"{verification['reason']}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, target_path)
    after = sha256_file(target_path)
    if after != verification["sha256"]:
        raise ValueError(
            "restored copy hash does not match the backup")
    return {
        "target": str(target_path),
        "sha256": after,
        "size_bytes": target_path.stat().st_size,
        "restored_at": _utcnow_iso(),
        "source_backup": str(backup_path),
    }


def list_backups(backup_dir=None):
    """Backups newest first, from the manifest.

    Backup files on disk that the manifest does not mention
    are included with a hash computed on the spot and a note.
    Returns {"backups": [...], "skipped_lines": n}.
    """
    backup_dir = Path(backup_dir) if backup_dir else DEFAULT_BACKUP_DIR
    records, skipped = _read_manifest(backup_dir)
    known = {record["backup_file"] for record in records}
    if backup_dir.exists():
        for path in sorted(backup_dir.glob("*.bak")):
            if path.name not in known:
                # Step 53: a pre-manifest backup has no recorded
                # created_at, so use the file's modification time
                # — otherwise the newest-first sort falls back
                # to the file name, which is not the backup's
                # age (the real 2026-11-08 backups sorted the
                # oldest one first).
                mtime = datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc).isoformat()
                records.append({
                    "backup_file": path.name,
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                    "created_at": mtime,
                    "source": None,
                    "label": None,
                    "integrity": "not in manifest",
                })
    # Sort by creation time, then by file name: several backups
    # made in the same second share a created_at, and the file
    # name embeds the backup timestamp, so the name breaks the
    # tie newest-first.
    records.sort(key=lambda r: (r.get("created_at") or "",
                                r.get("backup_file") or ""),
                 reverse=True)
    return {"backups": records, "skipped_lines": skipped}


def prune_backups(backup_dir=None, keep=5):
    """Delete all but the ``keep`` most recent backups.

    ``keep`` below 1 is refused, and the newest backup is
    never deleted.  Returns {"kept": [...], "deleted": [...]}
    with backup file names.  The manifest is rewritten to
    match what remains on disk.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1 (the newest "
                         "backup is never deleted)")
    backup_dir = Path(backup_dir) if backup_dir else DEFAULT_BACKUP_DIR
    listing = list_backups(backup_dir)
    backups = listing["backups"]
    kept = backups[:keep]
    deleted = backups[keep:]
    for record in deleted:
        path = backup_dir / record["backup_file"]
        if path.exists():
            path.unlink()
    _rewrite_manifest(backup_dir, kept)
    return {"kept": [r["backup_file"] for r in kept],
            "deleted": [r["backup_file"] for r in deleted]}


# ---------------------------------------------------------------------------
# JSON data-store snapshots
# ---------------------------------------------------------------------------

def backup_data_stores(data_dir, backup_dir=None, store_names=None,
                       label=None, now=None):
    """Snapshot the JSON data stores under ``data_dir``.

    Copies every existing store directory into a timestamped
    snapshot directory and writes ``manifest.json`` with a
    SHA-256 hash per file.  Missing store directories are
    listed as skipped, never an error.  Returns the snapshot
    record; the snapshot directory itself is the backup.
    """
    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir) if backup_dir else DEFAULT_BACKUP_DIR
    names = tuple(store_names) if store_names is not None \
        else DATA_STORE_DIRS
    stamp = now or _timestamp()
    snapshot_name = f"data-stores.{stamp}"
    if label:
        safe_label = "".join(
            ch if (ch.isalnum() or ch in "-_") else "-"
            for ch in str(label)).strip("-") or "snapshot"
        snapshot_name = f"data-stores.{safe_label}.{stamp}"
    snapshot_dir = backup_dir / snapshot_name
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    files = []
    skipped = []
    for name in names:
        source_dir = data_dir / name
        if not source_dir.is_dir():
            skipped.append(name)
            continue
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(data_dir).as_posix()
            dest = snapshot_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            files.append({
                "path": relative,
                "sha256": sha256_file(dest),
                "size_bytes": dest.stat().st_size,
            })
    manifest = {
        "snapshot": snapshot_name,
        "created_at": _utcnow_iso(),
        "data_dir": str(data_dir),
        "label": label,
        "files": files,
        "skipped_stores": skipped,
        "file_count": len(files),
    }
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8")
    return manifest


def verify_data_backup(snapshot_dir):
    """Verify a data-store snapshot against its manifest.

    Returns {"ok", "checked", "missing": [...],
    "mismatched": [...]}.  A snapshot with no manifest is
    reported, not crashed on.
    """
    snapshot_dir = Path(snapshot_dir)
    result = {"snapshot": str(snapshot_dir), "ok": False,
              "checked": 0, "missing": [], "mismatched": [],
              "reason": None}
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.exists():
        result["reason"] = "no manifest.json in the snapshot"
        return result
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result["reason"] = f"manifest.json is not valid JSON: {exc}"
        return result
    for entry in manifest.get("files", []):
        result["checked"] += 1
        path = snapshot_dir / entry["path"]
        if not path.exists():
            result["missing"].append(entry["path"])
        elif sha256_file(path) != entry.get("sha256"):
            result["mismatched"].append(entry["path"])
    result["ok"] = not result["missing"] and not result["mismatched"]
    result["reason"] = (
        "all files match the manifest" if result["ok"]
        else "files missing or changed since the snapshot")
    return result


# ---------------------------------------------------------------------------
# Small command line: python -m agents.backup_recovery <command>
# ---------------------------------------------------------------------------

def _cli(argv=None):
    parser = argparse.ArgumentParser(
        prog="agents.backup_recovery",
        description="Back up, verify, restore, list and prune "
                    "Jarvis database backups and data-store snapshots.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backup", help="back up a database")
    p.add_argument("db_path")
    p.add_argument("--backup-dir")
    p.add_argument("--label")

    p = sub.add_parser("verify", help="verify a backup file")
    p.add_argument("backup_path")
    p.add_argument("--expected-sha256")

    p = sub.add_parser("restore",
                       help="restore a backup to a named target "
                            "(never the production database)")
    p.add_argument("backup_path")
    p.add_argument("target_path")

    p = sub.add_parser("list", help="list backups, newest first")
    p.add_argument("--backup-dir")

    p = sub.add_parser("prune", help="keep only the N most recent")
    p.add_argument("--backup-dir")
    p.add_argument("--keep", type=int, default=5)

    p = sub.add_parser("backup-data",
                       help="snapshot the JSON data stores")
    p.add_argument("data_dir")
    p.add_argument("--backup-dir")
    p.add_argument("--label")

    p = sub.add_parser("verify-data",
                       help="verify a data-store snapshot")
    p.add_argument("snapshot_dir")

    args = parser.parse_args(argv)
    if args.command == "backup":
        print(json.dumps(backup_database(
            args.db_path, backup_dir=args.backup_dir,
            label=args.label), indent=2, sort_keys=True))
    elif args.command == "verify":
        print(json.dumps(verify_backup(
            args.backup_path,
            expected_sha256=args.expected_sha256),
            indent=2, sort_keys=True))
    elif args.command == "restore":
        print(json.dumps(restore_backup(
            args.backup_path, args.target_path),
            indent=2, sort_keys=True))
    elif args.command == "list":
        print(json.dumps(list_backups(args.backup_dir),
                         indent=2, sort_keys=True))
    elif args.command == "prune":
        print(json.dumps(prune_backups(
            args.backup_dir, keep=args.keep),
            indent=2, sort_keys=True))
    elif args.command == "backup-data":
        print(json.dumps(backup_data_stores(
            args.data_dir, backup_dir=args.backup_dir,
            label=args.label), indent=2, sort_keys=True))
    elif args.command == "verify-data":
        print(json.dumps(verify_data_backup(args.snapshot_dir),
                         indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
