"""Step 52 — backup, recovery and security systems (security half).

Three checks, all read-only:

1. Secrets scan — walks the repo tree (and, separately, the
   Git history) looking for likely secrets: private keys, API
   keys, tokens, passwords, bearer tokens and email addresses.
   A finding records ONLY the file, line and pattern name —
   never the matched text.  Nothing secret is ever printed,
   logged or written by this module, including in its own
   reports: counts and locations only.
2. Permissions check — reports the mode of the database and
   the JSON stores and flags anything world-writable.
3. Database-write audit — statically finds every
   ``sqlite3.connect`` call in the project, classifies it
   read-only (``mode=ro`` in the connection string) or
   write-capable, and works out which modules the integrated
   assistant actually loads (the ``main.py`` import graph).
   The documented set, from ``main.py``'s perspective, is
   exactly three write-capable paths (see
   ``DOCUMENTED_RUNTIME_WRITE_PATHS`` below): ``agent/memory.py``
   (the last-20-messages table), ``data/providers/
   provider_logger.py`` (the provider_logs table), and
   ``agents/backup_recovery.py`` — which writes only to a
   caller-chosen backup target file created by the SQLite
   backup API, never to the production database (its source
   is opened read-only, and restore refuses the production
   path).  The audit returns whether the code still matches
   that set, so a future write path cannot sneak in
   unnoticed.  Legacy modules outside
   the runtime (imports, collectors, the old alert manager
   and watchlist) open the database read-write by design and
   are listed separately, not hidden.

The audit is static: it reads source, it never imports the
project and never touches the database.
"""

import ast
import os
import re
import stat
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Directories the tree scan skips: version control, bytecode,
# and the large generated datasets (the 1.3 GB market-data
# cache and the 66 MB statement collection are not hand-written
# code and are far too large to scan line by line).
SCAN_EXCLUDE_DIRS = {
    ".git", "__pycache__", "data/market_data",
    "data/financial_statements",
}
# Files over this size are skipped, not read.
SCAN_MAX_FILE_BYTES = 1024 * 1024

# Pattern name -> regex.  A finding records the pattern name
# only; the matched text is never kept, printed or returned.
SECRET_PATTERNS = (
    ("private_key",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("api_key_assignment",
     re.compile(r"(?i)(?:api[_-]?key|apikey)\s*[:=]\s*['\"]?\S{8,}")),
    ("secret_assignment",
     re.compile(r"(?i)(?:secret|client_secret|access_token|auth_token"
                r"|password|passwd)\s*[:=]\s*['\"]?\S+")),
    ("bearer_token",
     re.compile(r"(?i)Authorization:\s*Bearer\s+\S+")),
    ("openai_key",
     re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9-]{7,}")),
    ("aws_access_key",
     re.compile(r"AKIA[0-9A-Z]{16}")),
    ("email_address",
     re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
)

# The documented write-capable database paths in the integrated
# assistant's runtime, relative to the project root.  Step 52
# correction: the set is TWO paths, not one.  The audit caught
# a second write path the earlier steps had missed —
# data/providers/provider_logger.py logs every provider request
# (tools/finance.get_company_financials, wrapped as a Step 44
# tool) into the ``provider_logs`` table, and that table
# already holds 1 row in the production database.  So:
# agent/memory.py writes the conversations table, and
# provider_logger.py writes provider_logs; everything else the
# runtime loads opens the database read-only.  Both paths are
# now governed by the session database (Step 52 wiring in
# main.py), so a --db-path session writes to neither the
# production conversations table nor the production
# provider_logs table.
DOCUMENTED_RUNTIME_WRITE_PATHS = (
    "agent/memory.py",
    "data/providers/provider_logger.py",
    # Step 53: the deployment readiness check imports the
    # backup module, so it joined the runtime.  Its only
    # write-capable connect is the backup *destination* file
    # (sqlite3.connect(target) in backup_database) — a new
    # file created by the SQLite backup API from a read-only
    # source.  It never writes to the production database.
    "agents/backup_recovery.py",
)


def _is_binary(path):
    try:
        with open(path, "rb") as handle:
            return b"\x00" in handle.read(4096)
    except OSError:
        return True


def _scan_lines(lines, rel_file, kind, commit=None):
    """Apply SECRET_PATTERNS to lines; return findings.

    ``lines`` is an iterable of (line_number, text).  Findings
    carry file, line, pattern and kind (and commit for history)
    — never the matched text.
    """
    findings = []
    for number, text in lines:
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                finding = {"kind": kind, "file": rel_file,
                           "line": number, "pattern": name}
                if commit is not None:
                    finding["commit"] = commit
                findings.append(finding)
    return findings


def scan_tree(root=None):
    """Scan the working tree for likely secrets.

    Returns {"findings", "files_scanned", "files_skipped",
    "skipped_large", "excluded_dirs"}.  Findings are
    {kind, file, line, pattern} — no secret values.
    """
    root = Path(root) if root else PROJECT_ROOT
    findings = []
    scanned = 0
    skipped = 0
    skipped_large = 0
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        rel_dir = current.relative_to(root).as_posix()
        dirnames[:] = [
            d for d in dirnames
            if (rel_dir + "/" + d if rel_dir != "." else d)
            not in SCAN_EXCLUDE_DIRS and d not in SCAN_EXCLUDE_DIRS]
        for name in sorted(filenames):
            path = current / name
            rel = path.relative_to(root).as_posix()
            if path.stat().st_size > SCAN_MAX_FILE_BYTES:
                skipped_large += 1
                continue
            if _is_binary(path):
                skipped += 1
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                skipped += 1
                continue
            scanned += 1
            findings.extend(_scan_lines(
                enumerate(text.splitlines(), start=1), rel, "tree"))
    return {"findings": findings, "files_scanned": scanned,
            "files_skipped": skipped, "skipped_large": skipped_large,
            "excluded_dirs": sorted(SCAN_EXCLUDE_DIRS)}


def scan_git_history(repo_root=None):
    """Scan added lines in the Git history for likely secrets.

    Runs ``git log --all -p`` and applies the same patterns to
    added lines only.  A finding records the short commit hash,
    file, new-file line number and pattern name — never the
    matched text, so a secret that lives in history is located
    without being reproduced.  Returns {"findings",
    "commits_scanned"}; an empty history or a non-repo returns
    no findings with a reason.
    """
    repo_root = Path(repo_root) if repo_root else PROJECT_ROOT
    result = {"findings": [], "commits_scanned": 0, "reason": None}
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--all",
             "--format=COMMIT:%H", "-p", "--", "."],
            capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["reason"] = f"git log could not run: {exc}"
        return result
    if proc.returncode != 0:
        result["reason"] = "not a git repository or no history"
        return result
    findings = []
    commits = 0
    commit = None
    current_file = None
    new_line = 0
    for raw in proc.stdout.splitlines():
        if raw.startswith("COMMIT:"):
            commit = raw[len("COMMIT:"):][:12]
            commits += 1
            current_file = None
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                current_file = None
            else:
                current_file = (target[2:] if target.startswith("b/")
                                else target)
            continue
        if raw.startswith("@@ "):
            match = re.search(r"\+(\d+)", raw)
            new_line = int(match.group(1)) if match else 0
            continue
        if current_file is None or commit is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            findings.extend(_scan_lines(
                [(new_line, raw[1:])], current_file, "history",
                commit=commit))
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        elif raw.startswith(" ") or raw == "":
            new_line += 1
    result["findings"] = findings
    result["commits_scanned"] = commits
    return result


def check_permissions(paths):
    """Report the mode of each path; flag world-writable ones.

    Returns a list of {"path", "mode", "world_writable",
    "group_writable", "ok", "exists"}.  ``ok`` is False only
    for a world-writable path; group-writable is reported but
    not failed (the project files are group ``nogroup`` with
    mode 660 by convention).
    """
    results = []
    for raw in paths:
        path = Path(raw)
        record = {"path": str(path), "mode": None,
                  "world_writable": None, "group_writable": None,
                  "ok": False, "exists": path.exists()}
        if not path.exists():
            record["reason"] = "path does not exist"
            results.append(record)
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        record["mode"] = oct(mode)
        record["world_writable"] = bool(mode & 0o002)
        record["group_writable"] = bool(mode & 0o020)
        record["ok"] = not record["world_writable"]
        results.append(record)
    return results


# ---------------------------------------------------------------------------
# Database-write audit (static)
# ---------------------------------------------------------------------------

def _connect_is_read_only(call):
    """True when a sqlite3.connect call provably opens read-only.

    Read-only means the connection string carries ``mode=ro``
    — as a plain string or inside an f-string's constant parts.
    A variable path cannot be proven read-only statically and
    counts as write-capable.
    """
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return "mode=ro" in first.value
    if isinstance(first, ast.JoinedStr):
        return any(isinstance(part, ast.Constant)
                   and isinstance(part.value, str)
                   and "mode=ro" in part.value
                   for part in first.values)
    return False


def _find_connect_sites(path):
    """sqlite3.connect call sites in one file.

    Returns a list of {"file", "line", "read_only", "reason"}.
    Unparseable files are reported as a site with reason
    "unparseable" so they are visible, not silently skipped.
    """
    rel = path.relative_to(PROJECT_ROOT).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return [{"file": rel, "line": 0, "read_only": False,
                 "reason": "file could not be parsed"}]
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and func.attr == "connect"
                and isinstance(func.value, ast.Name)
                and func.value.id == "sqlite3"):
            continue
        read_only = _connect_is_read_only(node)
        sites.append({
            "file": rel, "line": node.lineno,
            "read_only": read_only,
            "reason": ("mode=ro in the connection string" if read_only
                       else "no mode=ro in the connection call "
                            "(variable or plain path)")})
    return sites


def _module_file(dotted, current_file):
    """Resolve a dotted module name to a project file, or None.

    Handles top-level packages (agent, agents, tools, data,
    main) and relative imports inside them.
    """
    parts = dotted.split(".") if dotted else []
    if not parts:
        return None
    base = {"agent": "agent", "agents": "agents",
            "tools": "tools", "data": "data"}.get(parts[0])
    if base is None:
        if parts[0] == "main":
            return PROJECT_ROOT / "main.py"
        return None
    candidate = PROJECT_ROOT / base / "/".join(parts[1:])
    if (candidate.with_suffix(".py")).exists():
        return candidate.with_suffix(".py")
    if (candidate / "__init__.py").exists():
        return candidate / "__init__.py"
    return None


def _local_imports(path):
    """Dotted module names a file imports, resolved to files."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()
    rel = path.relative_to(PROJECT_ROOT)
    package = rel.parts[0] if len(rel.parts) > 1 else ""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = _module_file(alias.name, path)
                if resolved is not None:
                    found.add(resolved)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            if level == 0:
                module = node.module or ""
                resolved = _module_file(module, path)
                if resolved is not None:
                    found.add(resolved)
                # "from agents import x" — x may be a submodule.
                if module in ("agent", "agents", "tools", "data"):
                    for alias in node.names:
                        resolved = _module_file(
                            f"{module}.{alias.name}", path)
                        if resolved is not None:
                            found.add(resolved)
            else:
                # Relative: level 1 is the current package.
                pkg_parts = [package] if package else []
                up = level - 1
                base_parts = pkg_parts[:len(pkg_parts) - up] \
                    if up <= len(pkg_parts) else []
                base = ".".join(base_parts)
                module = node.module or ""
                dotted = (base + "." + module) if module else base
                if dotted:
                    resolved = _module_file(dotted, path)
                    if resolved is not None:
                        found.add(resolved)
                    if not module:
                        for alias in node.names:
                            resolved = _module_file(
                                f"{dotted}.{alias.name}", path)
                            if resolved is not None:
                                found.add(resolved)
    return found


def _runtime_modules():
    """Project files reachable from main.py through local imports."""
    start = PROJECT_ROOT / "main.py"
    seen = set()
    queue = [start]
    while queue:
        current = queue.pop()
        if current in seen or not current.exists():
            continue
        seen.add(current)
        for nxt in _local_imports(current):
            if nxt not in seen:
                queue.append(nxt)
    return seen


def audit_db_write_paths(project_root=None):
    """Audit every sqlite3.connect site in the project.

    Returns {"write_capable_sites", "runtime_write_capable_sites",
    "runtime_modules", "documented_runtime_set",
    "matches_documented_set", "read_only_sites",
    "excluded"}.  ``write_capable_sites`` is every connect call
    without a provable ``mode=ro``; ``runtime_*`` restricts to
    modules the integrated assistant actually loads from
    main.py.  Test/check scripts are excluded from the scan
    (they run against temporary databases by construction).
    """
    root = Path(project_root) if project_root else PROJECT_ROOT
    search_dirs = [root / "agent", root / "agents", root / "tools",
                   root / "data"]
    search_files = [root / "main.py"]
    for directory in search_dirs:
        if directory.is_dir():
            search_files.extend(sorted(directory.rglob("*.py")))
    sites = []
    for path in search_files:
        if "__pycache__" in path.parts:
            continue
        if path.name.startswith(("test_", "check_")):
            continue
        sites.extend(_find_connect_sites(path))
    write_capable = [s for s in sites if not s["read_only"]]
    read_only = [s for s in sites if s["read_only"]]

    runtime = _runtime_modules()
    runtime_files = {p.relative_to(root).as_posix() for p in runtime}
    runtime_write = [s for s in write_capable
                     if s["file"] in runtime_files]
    documented = set(DOCUMENTED_RUNTIME_WRITE_PATHS)
    found = {s["file"] for s in runtime_write}
    return {
        "write_capable_sites": write_capable,
        "runtime_write_capable_sites": runtime_write,
        "runtime_modules": sorted(runtime_files),
        "documented_runtime_set": sorted(documented),
        "matches_documented_set": found == documented,
        "read_only_sites": read_only,
        "excluded": ["test_*.py and check_*.py scripts "
                     "(temporary databases by construction)",
                     "__pycache__ directories"],
    }
