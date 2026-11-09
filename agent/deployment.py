"""Step 53 — deploy reliable always-on assistant: readiness checks.

A deployment checklist and readiness report over the real
project state.  Everything here is read-only: it opens the
database read-only, lists (never takes) backups, runs the
Step 52 secrets scan, and probes Ollama with a plain TCP
connect to 127.0.0.1:11434 — no network beyond localhost, no
background process, no cron, no service, no file is created,
modified or deleted by this module.

Checks (each returns ``{"name", "status", "detail",
"evidence"}`` with ``status`` one of ``pass`` / ``warning`` /
``fail`` / ``unknown``):

- ``database`` — integrity_check + SHA-256 + row counts.
- ``backups`` — the newest backup in the Step 52 backup
  directory, verified (hash + integrity).  Backups that
  predate the Step 52 manifest verify by integrity only and
  are reported as such, never as manifest-verified.
- ``tests`` — the test suite.  By default this check does
  NOT run the suite (it takes ~2 minutes); it reports
  ``unknown`` with the command to run.  Pass ``run=True`` to
  run it and parse the result — never claim a pass that was
  not run.
- ``ollama`` — is the local model reachable on localhost?
- ``cloud`` — is a cloud model configured?  Counts providers
  only; a key is never printed, logged or returned.
- ``secrets`` — the Step 52 tree scan (plus Git history when
  asked).  Findings are counts and locations only, never
  values.  Known-accepted findings (the local untracked
  ``.env``, the ``.env.example`` placeholder, test fixtures)
  are listed by file, not hidden.
- ``ui`` — the Step 48 UI binds localhost only (static
  check of ``agent/ui.py``: default host and the refusal of
  any other host).
- ``guardrails`` — the Step 39/44 refusal paths exist in
  source (trade and send/schedule guardrails, forbidden
  tool names).
- ``data_gaps`` — what the data actually supports: universe
  rows, tickers with standardised statement line items
  (``revenue`` in the stored JSON), latest price snapshot,
  holdings store, voice preference.

The verdict is honest by construction:
``readiness_report`` returns ``"ready_for_always_on"`` only
when every check passes AND the caller supplies evidence
that the four live checks have actually been done on the
deployment machine (a live Ollama turn through the
integrated ``main.py``, a live backup taken with the Step 52
CLI and verified, a live UI session in a browser, a live
restore drill).  With no such evidence — the normal case on
this machine, where no Ollama exists and no live turn has
ever run — the verdict is ``"not_ready_for_always_on"`` and
the pending live checks are listed as blockers.  Nothing
here inflates readiness.

``startup_check_lines`` is the cheap subset ``main.py`` runs
after its banner: database integrity + hash, backup
directory contents, Ollama reachability — one line each,
never blocking startup on a failure, never printing a
secret.
"""

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "database" / "assistant.db"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

OLLAMA_HOST = "127.0.0.1"
OLLAMA_PORT = 11434

# A ticker counts as having standardised statement line items
# when at least one stored statement row carries a "revenue"
# key in its JSON.  D05 rows are announcement metadata only
# (announcement_id, pdf_url, extraction_status — no line
# items), so D05 does not count; MSFT does.
STATEMENT_LINE_ITEM_KEY = "revenue"

# The four live checks that only the deployment machine can
# prove.  All default to not done.
LIVE_CHECKS = (
    "live_ollama_turn",       # a live turn through integrated main.py
    "live_backup_verified",   # a backup taken with the Step 52 CLI + verified
    "live_ui_session",        # the UI opened in a real browser
    "live_restore_drill",     # a restore drill against a real backup
)


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _check(name, status, detail, evidence=None):
    return {"name": name, "status": status, "detail": detail,
            "evidence": evidence or {}}


# ---------------------------------------------------------------------------
# Ollama reachability (localhost only)
# ---------------------------------------------------------------------------

def ollama_reachable(host=OLLAMA_HOST, port=OLLAMA_PORT, timeout=1.0):
    """True when something answers on the local Ollama port.

    A plain TCP connect to 127.0.0.1 — no HTTP request, no
    network beyond localhost, no model call.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _sha256_file(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_database(db_path=None):
    """Integrity, hash and row counts.  Read-only throughout."""
    db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    if not db_path.exists():
        return _check("database", "fail",
                      f"database not found: {db_path}", {})
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            integrity = conn.execute(
                "PRAGMA integrity_check").fetchone()[0]
            universe = conn.execute(
                "SELECT COUNT(*) FROM market_universe").fetchone()[0]
            snapshots = conn.execute(
                "SELECT COUNT(*) FROM financial_snapshots").fetchone()[0]
            latest = conn.execute(
                "SELECT MAX(snapshot_date) FROM financial_snapshots"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT ticker, data_json FROM financial_statements"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _check("database", "fail",
                      f"database could not be read: {exc}", {})
    with_items = set()
    for ticker, raw in rows:
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and STATEMENT_LINE_ITEM_KEY in data:
            with_items.add(ticker)
    evidence = {
        "path": str(db_path),
        "sha256": _sha256_file(db_path),
        "integrity": integrity,
        "universe_rows": universe,
        "statement_rows": len(rows),
        "tickers_with_line_items": sorted(with_items),
        "snapshot_rows": snapshots,
        "latest_snapshot": str(latest)[:10] if latest else None,
    }
    if integrity != "ok":
        return _check("database", "fail",
                      f"integrity_check failed: {integrity}", evidence)
    return _check(
        "database", "pass",
        f"integrity ok; sha256 {evidence['sha256'][:12]}…; "
        f"{universe} universe rows; "
        f"{len(with_items)} ticker(s) with statement line items "
        f"({', '.join(sorted(with_items)) or 'none'}); "
        f"latest price snapshot "
        f"{evidence['latest_snapshot'] or 'unknown'}",
        evidence)


# ---------------------------------------------------------------------------
# Backups (Step 52)
# ---------------------------------------------------------------------------

def check_backups(backup_dir=None):
    """Newest backup in the Step 52 backup directory, verified.

    Never takes a backup — listing and verifying only.
    """
    from agents.backup_recovery import (
        DEFAULT_BACKUP_DIR, list_backups, verify_backup)
    backup_dir = Path(backup_dir) if backup_dir else DEFAULT_BACKUP_DIR
    if not backup_dir.exists():
        return _check("backups", "fail",
                      f"backup directory does not exist: {backup_dir}",
                      {"backup_dir": str(backup_dir)})
    listing = list_backups(backup_dir)
    backups = listing.get("backups") or []
    if not backups:
        return _check("backups", "fail",
                      "no backups in the backup directory",
                      {"backup_dir": str(backup_dir), "count": 0})
    newest = backups[0]
    path = backup_dir / newest["backup_file"]
    verification = verify_backup(path)
    try:
        age_days = (datetime.now(timezone.utc)
                    - datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc)).days
    except OSError:
        age_days = None
    evidence = {
        "backup_dir": str(backup_dir),
        "count": len(backups),
        "newest": newest["backup_file"],
        "newest_size_bytes": newest.get("size_bytes"),
        "newest_age_days": age_days,
        "newest_in_manifest": newest.get("integrity") != "not in manifest",
        "newest_verified": verification.get("ok"),
        "newest_sha256": verification.get("sha256"),
    }
    if not verification.get("ok"):
        return _check(
            "backups", "fail",
            f"newest backup {newest['backup_file']} does not verify: "
            f"{verification.get('reason')}", evidence)
    if not evidence["newest_in_manifest"]:
        return _check(
            "backups", "warning",
            f"{len(backups)} backup(s); newest "
            f"{newest['backup_file']} verifies (integrity ok) but "
            f"predates the Step 52 manifest — no recorded hash to "
            f"compare against. Take a fresh backup with "
            f"`python -m agents.backup_recovery backup "
            f"database/assistant.db --label <reason>`.",
            evidence)
    if age_days is not None and age_days > 7:
        return _check(
            "backups", "warning",
            f"newest backup {newest['backup_file']} verifies but is "
            f"{age_days} days old — take a fresh one before "
            f"deploying.", evidence)
    return _check(
        "backups", "pass",
        f"{len(backups)} backup(s); newest "
        f"{newest['backup_file']} verifies (hash + integrity).",
        evidence)


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

def check_tests(run=False, timeout=600, command=None):
    """Test-suite status.

    ``run=False`` (default) reports ``unknown`` — the suite
    takes ~2 minutes and this check must not claim a pass it
    did not run.  ``run=True`` runs
    ``python -m unittest discover`` (or ``command``) in a
    subprocess and parses its output.
    """
    if not run:
        return _check(
            "tests", "unknown",
            "suite not run by this check — run "
            "`python -m unittest discover` from the project root "
            "and record the result; a pass must be observed, not "
            "assumed.", {})
    cmd = list(command) if command else [
        sys.executable, "-m", "unittest", "discover"]
    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), capture_output=True,
            text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check("tests", "fail",
                      f"test suite could not run: {exc}", {})
    output = (proc.stdout or "") + (proc.stderr or "")
    ran = re.search(r"Ran (\d+) tests?", output)
    skipped = re.search(r"skipped=(\d+)", output)
    evidence = {
        "command": " ".join(cmd),
        "tests_run": int(ran.group(1)) if ran else None,
        "skipped": int(skipped.group(1)) if skipped else None,
        "exit_code": proc.returncode,
    }
    if proc.returncode == 0 and "OK" in output:
        return _check(
            "tests", "pass",
            f"suite passed: {evidence['tests_run']} tests "
            f"({evidence['skipped'] or 0} skipped).", evidence)
    return _check(
        "tests", "fail",
        "suite did not pass (see output; exit code "
        f"{proc.returncode}).", evidence)


# ---------------------------------------------------------------------------
# Ollama / cloud
# ---------------------------------------------------------------------------

def check_ollama(timeout=1.0):
    """Is the local Ollama model reachable on localhost?"""
    if ollama_reachable(timeout=timeout):
        return _check(
            "ollama", "pass",
            f"local Ollama answers on {OLLAMA_HOST}:{OLLAMA_PORT}.",
            {"host": OLLAMA_HOST, "port": OLLAMA_PORT})
    return _check(
        "ollama", "fail",
        f"local Ollama not reachable on {OLLAMA_HOST}:{OLLAMA_PORT} "
        "— start it (`ollama serve`) before deploying; chat "
        "turns will report 'unreachable' until then.",
        {"host": OLLAMA_HOST, "port": OLLAMA_PORT})


def check_cloud(config_path=None):
    """Cloud-model config: present or absent.  Never prints a key."""
    from agent.cloud_models import load_config
    try:
        config = load_config(config_path)
    except ValueError as exc:
        return _check("cloud", "fail",
                      f"cloud config is invalid: {exc}", {})
    providers = config.get("providers") or []
    cloud = [p for p in providers
             if (p.get("kind") or "cloud").lower() == "cloud"]
    evidence = {"cloud_providers": len(cloud),
                "total_providers": len(providers)}
    if not cloud:
        return _check(
            "cloud", "pass",
            "no cloud model configured (local Ollama only) — "
            "the expected state; no key exists to leak or rotate.",
            evidence)
    return _check(
        "cloud", "pass",
        f"{len(cloud)} cloud provider(s) configured; "
        "keys are read from environment variables only and are "
        "never printed here.", evidence)


# ---------------------------------------------------------------------------
# Secrets (Step 52 scan)
# ---------------------------------------------------------------------------

def check_secrets(project_root=None, include_history=True):
    """Step 52 secrets scan: counts and locations only."""
    from agents.security import scan_git_history, scan_tree
    root = Path(project_root) if project_root else PROJECT_ROOT
    tree = scan_tree(root)
    findings = list(tree.get("findings") or [])
    history = {"findings": [], "commits_scanned": 0}
    if include_history:
        history = scan_git_history(root)
        findings.extend(history.get("findings") or [])
    by_file = {}
    for finding in findings:
        by_file.setdefault(finding["file"], 0)
        by_file[finding["file"]] += 1
    evidence = {
        "tree_files_scanned": tree.get("files_scanned"),
        "tree_findings": len(tree.get("findings") or []),
        "history_commits_scanned": history.get("commits_scanned"),
        "history_findings": len(history.get("findings") or []),
        "findings_by_file": by_file,
    }
    if not findings:
        return _check("secrets", "pass",
                      "secrets scan clean: no findings in the tree "
                      "or Git history.", evidence)
    return _check(
        "secrets", "warning",
        f"secrets scan: {len(tree.get('findings') or [])} tree "
        f"finding(s), {len(history.get('findings') or [])} history "
        f"finding(s). Known-accepted: the local untracked .env "
        f"(gitignored, correct state), the .env.example "
        f"placeholder, and test fixtures with fake values. "
        f"History still holds the deleted .env and the old "
        f"hardcoded contact value — scrubbing published history "
        f"is the owner's call. No secret value is reproduced "
        f"here.", evidence)


# ---------------------------------------------------------------------------
# UI binding + guardrails (static source checks)
# ---------------------------------------------------------------------------

def check_ui(project_root=None):
    """The Step 48 UI binds localhost only."""
    root = Path(project_root) if project_root else PROJECT_ROOT
    source = (root / "agent" / "ui.py").read_text(encoding="utf-8")
    evidence = {"default_host": None, "refuses_other_hosts": False}
    match = re.search(r'DEFAULT_HOST\s*=\s*["\']([^"\']+)["\']', source)
    if match:
        evidence["default_host"] = match.group(1)
    evidence["refuses_other_hosts"] = (
        "refused: the Jarvis UI binds localhost only" in source)
    if (evidence["default_host"] in ("127.0.0.1", "localhost")
            and evidence["refuses_other_hosts"]):
        return _check(
            "ui", "pass",
            "UI binds localhost only (default "
            f"{evidence['default_host']}); make_server refuses any "
            "other host.", evidence)
    return _check(
        "ui", "fail",
        "UI localhost binding could not be confirmed in source.",
        evidence)


def check_guardrails(project_root=None):
    """The Step 39/44 refusal paths exist in source."""
    root = Path(project_root) if project_root else PROJECT_ROOT
    orch = (root / "agent" / "orchestrator.py").read_text(
        encoding="utf-8")
    tools = (root / "agent" / "tool_calling.py").read_text(
        encoding="utf-8")
    evidence = {
        "trade_guardrail": "guardrail_trade" in orch,
        "send_schedule_guardrail": "guardrail_schedule_send" in orch,
        "trade_refusal_text": "I will not place a trade" in orch,
        "forbidden_tool_names": "ForbiddenToolError" in tools,
    }
    missing = [key for key, ok in evidence.items() if not ok]
    if not missing:
        return _check(
            "guardrails", "pass",
            "trade and send/schedule guardrails present; "
            "trade/send/schedule tool names refused at "
            "registration; no order is ever placed without the "
            "owner's approval for that exact order.", evidence)
    return _check(
        "guardrails", "fail",
        "guardrail(s) missing from source: "
        + ", ".join(missing) + ".", evidence)


# ---------------------------------------------------------------------------
# Data gaps
# ---------------------------------------------------------------------------

def _voice_preference(data_dir):
    """Stored voice preference, read from the Step 43 store."""
    memory_dir = Path(data_dir) / "memory"
    if not memory_dir.is_dir():
        return "unknown (no long-term memory store)"
    for path in sorted(memory_dir.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("status") != "active":
            continue
        tags = record.get("tags") or []
        if "voice" in tags:
            return str(record.get("voice_mode") or "text")
    return "unknown (no voice preference record)"


def check_data_gaps(db_path=None, data_dir=None):
    """What the data actually supports — stated, not assumed."""
    data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    db = check_database(db_path)
    evidence = dict(db.get("evidence") or {})
    holdings = data_dir / "holdings"
    evidence["holdings_store_exists"] = holdings.exists()
    evidence["voice_preference"] = _voice_preference(data_dir)
    gaps = []
    tickers = evidence.get("tickers_with_line_items") or []
    if len(tickers) <= 1:
        gaps.append(
            f"only {len(tickers)} ticker(s) with statement line "
            f"items ({', '.join(tickers) or 'none'}) of "
            f"{evidence.get('universe_rows')} universe rows — "
            "everything else is insufficient_data")
    if not evidence.get("latest_snapshot"):
        gaps.append("no price snapshot at all")
    elif evidence["latest_snapshot"] != datetime.now(
            timezone.utc).date().isoformat():
        gaps.append(
            "no live price feed (latest snapshot "
            f"{evidence['latest_snapshot']})")
    if not evidence["holdings_store_exists"]:
        gaps.append("no holdings store — the owner has never "
                    "entered his holdings")
    if evidence["voice_preference"] != "voice":
        gaps.append(
            "voice off by default (stored preference "
            f"'{evidence['voice_preference']}')")
    if db["status"] != "pass":
        return _check("data_gaps", "fail",
                      "database check failed, so the data gaps "
                      "cannot be assessed.", evidence)
    if not gaps:
        return _check("data_gaps", "pass",
                      "no data gaps found.", evidence)
    return _check("data_gaps", "warning",
                  "Data gaps: " + "; ".join(gaps) + ".", evidence)


# ---------------------------------------------------------------------------
# Readiness report
# ---------------------------------------------------------------------------

def readiness_report(project_root=None, db_path=None, data_dir=None,
                     backup_dir=None, run_tests=False,
                     test_command=None, live_checks=None):
    """One readiness report over the real project state.

    ``live_checks`` is a dict naming which of the four live
    checks (see LIVE_CHECKS) the owner has actually done on
    the deployment machine — evidence, not assertion.  The
    verdict is ``"ready_for_always_on"`` only when every
    check passes AND all four live checks are done.  Anything
    else is ``"not_ready_for_always_on"`` with the blockers
    listed.
    """
    root = Path(project_root) if project_root else PROJECT_ROOT
    db = Path(db_path) if db_path else DEFAULT_DB_PATH
    data = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    done = dict.fromkeys(LIVE_CHECKS, False)
    if live_checks:
        for key in LIVE_CHECKS:
            done[key] = bool(live_checks.get(key))
    checks = [
        check_database(db),
        check_backups(backup_dir),
        check_tests(run=run_tests, command=test_command),
        check_ollama(),
        check_cloud(),
        check_secrets(root),
        check_ui(root),
        check_guardrails(root),
        check_data_gaps(db, data),
    ]
    blockers = []
    for check in checks:
        if check["status"] == "fail":
            blockers.append(
                f"{check['name']}: {check['detail']}")
        elif check["status"] in ("warning", "unknown"):
            blockers.append(
                f"{check['name']} ({check['status']}): "
                f"{check['detail']}")
    pending_live = [key for key in LIVE_CHECKS if not done[key]]
    for key in pending_live:
        blockers.append(
            f"live check not done on the deployment machine: {key}")
    verdict = ("ready_for_always_on"
               if not blockers else "not_ready_for_always_on")
    return {
        "generated_at": _utcnow_iso(),
        "project_root": str(root),
        "verdict": verdict,
        "checks": checks,
        "live_checks_done": done,
        "blockers": blockers,
    }


def summary_lines(report):
    """One line per check plus the verdict, for CLI / /dashboard."""
    lines = [f"Deployment readiness — {report['verdict']} "
             f"({report['generated_at'][:19]}Z)"]
    for check in report["checks"]:
        lines.append(f"[{check['status']}] {check['name']}: "
                     f"{check['detail']}")
    if report["blockers"]:
        lines.append(
            f"Blockers ({len(report['blockers'])}):")
        for blocker in report["blockers"]:
            lines.append(f"- {blocker}")
    else:
        lines.append("No blockers — ready for always-on deployment.")
    return lines


def report_to_markdown(report):
    """The readiness report as Markdown (a file, not a send)."""
    lines = ["# Jarvis deployment readiness",
             "",
             f"Generated: {report['generated_at']}",
             "",
             f"**Verdict: {report['verdict']}**",
             "",
             "## Checks",
             ""]
    for check in report["checks"]:
        lines.append(f"- **{check['name']}** "
                     f"({check['status']}): {check['detail']}")
    lines.append("")
    lines.append("## Live checks")
    lines.append("")
    for key in LIVE_CHECKS:
        done = report["live_checks_done"].get(key, False)
        lines.append(f"- {'[x]' if done else '[ ]'} {key}")
    lines.append("")
    if report["blockers"]:
        lines.append("## Blockers")
        lines.append("")
        for blocker in report["blockers"]:
            lines.append(f"- {blocker}")
    else:
        lines.append("No blockers.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Startup self-check (main.py runs this after the banner)
# ---------------------------------------------------------------------------

def startup_check_lines(db_path=None, backup_dir=None,
                        ollama_timeout=0.5):
    """Cheap readiness lines for startup.  Never raises.

    One line each: database integrity + hash, backup
    directory contents, Ollama reachability.  A failure is
    reported, never fatal — startup is never blocked by this
    check — and nothing secret is printed.
    """
    lines = []
    try:
        db = check_database(db_path)
        evidence = db.get("evidence") or {}
        if db["status"] == "pass":
            lines.append(
                "Self-check: database integrity ok "
                f"(sha256 {evidence.get('sha256', '')[:12]}…, "
                f"{evidence.get('universe_rows')} universe rows)")
        else:
            lines.append(
                f"Self-check: database problem — {db['detail']}")
    except Exception as exc:  # noqa: BLE001 - self-check must
        # never take the startup down, whatever goes wrong.
        lines.append(f"Self-check: database check failed ({exc})")
    try:
        from agents.backup_recovery import DEFAULT_BACKUP_DIR
        directory = Path(backup_dir) if backup_dir \
            else DEFAULT_BACKUP_DIR
        if directory.is_dir():
            count = len(list(directory.glob("*.bak")))
            lines.append(
                f"Self-check: backup directory {directory} holds "
                f"{count} backup(s)")
        else:
            lines.append(
                f"Self-check: no backup directory at {directory} — "
                "take a backup before relying on this deployment")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Self-check: backup check failed ({exc})")
    try:
        if ollama_reachable(timeout=ollama_timeout):
            lines.append(
                "Self-check: local Ollama reachable "
                f"({OLLAMA_HOST}:{OLLAMA_PORT})")
        else:
            lines.append(
                "Self-check: local Ollama not reachable "
                f"({OLLAMA_HOST}:{OLLAMA_PORT}) — start it with "
                "`ollama serve`; chat turns will report "
                "'unreachable' until then")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Self-check: Ollama check failed ({exc})")
    return lines
