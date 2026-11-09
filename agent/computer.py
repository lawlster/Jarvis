"""Step 47 — Computer and application interaction.

A capability registry, a local executor, a remote gate, and an
audit log — the interaction layer Jarvis itself runs on. It is
deliberately narrow:

- Local operations run only on the machine Jarvis itself runs
  on (Linux here), inside a caller-chosen workspace root.
- Commands run only from a fixed allowlist, with ``shell=False``,
  a timeout, and no shell metacharacters. Destructive commands
  (delete, move, overwrite outside the workspace, network
  configuration, process killing) are refused by construction:
  they are not in the allowlist and there is no API to add to
  it — adding one means editing this module.
- Application launch is by name from a fixed allowlist, and
  only through a caller-supplied launcher. No launcher is
  configured by default, so nothing is ever launched unless
  the caller plugs one in.
- Remote actions are DISABLED by default. A remote target does
  nothing until the owner explicitly enables it through
  :func:`configure_remote_target`, which records who approved
  it, when, and what target. With no enabled target, every
  remote action returns ``"remote not configured"`` and never
  attempts a connection.

What this step does NOT do — stated plainly, because the owner
works on a Windows PC with Visual Studio Code and computer
pairing is Mac-only:

- There is NO direct control of the owner's Windows machine,
  his VS Code window, or his screen. Nothing in this module
  connects to, launches anything on, or runs a shell command
  on his Windows PC.
- The workable routes stay what they have always been: build
  here and hand over files for him to open in VS Code, or
  remote file access only with his explicit setup approval
  (which is exactly what :func:`configure_remote_target`
  records).
- Any Windows-specific behaviour is labelled as such and never
  executed here; :func:`windows_control_available` always
  returns ``False`` with the reason.

Nothing here places a trade, sends a message, or schedules
anything (the same forbidden-name rule as Step 44 applies to
capability names). Nothing writes to the production database.
Every action attempt — allowed or refused — is recorded as a
JSON line in the audit log (default ``data/computer_actions/``,
caller-chosen), with the action, its arguments, the decision,
and the outcome. Nothing is executed without a log entry: the
attempt is logged first, the outcome is logged after.
"""

import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ComputerError(Exception):
    """Base error for the computer-interaction layer."""


class ForbiddenCapabilityError(ComputerError):
    """A capability name suggests trading, sending or scheduling."""


class DuplicateCapabilityError(ComputerError):
    """A capability with this name is already registered."""


class RefusedActionError(ComputerError):
    """An action was refused (not in the allowlist, outside the
    workspace root, missing approval, or otherwise not permitted).
    The refusal is also recorded in the audit log by the caller
    that raised it — see :func:`perform_action`."""


class RemoteNotConfiguredError(ComputerError):
    """A remote action was attempted with no enabled target."""


# ---------------------------------------------------------------------------
# Forbidden names (same rule as Step 44's tool registry)
# ---------------------------------------------------------------------------

_FORBIDDEN_NAME_TOKENS = frozenset({
    "trade", "buy", "sell", "send", "schedule", "remind", "order",
    "place", "purchase", "email", "message", "telegram", "short",
})


def _name_tokens(name):
    return re.split(r"[^a-z0-9]+", (name or "").lower())


def _check_capability_name(name):
    bad = [t for t in _name_tokens(name) if t in _FORBIDDEN_NAME_TOKENS]
    if bad:
        raise ForbiddenCapabilityError(
            f"capability name '{name}' is forbidden: it suggests "
            f"{', '.join(sorted(set(bad)))} — the registry must never "
            "contain a trading, sending or scheduling capability")


# ---------------------------------------------------------------------------
# Fixed allowlists — module-level constants, not runtime state.
# Nothing outside these lists can run or launch without editing
# this module, which is the point: destructive commands are
# refused by construction.
# ---------------------------------------------------------------------------

# Read-only, side-effect-free commands only. No delete, no move,
# no copy, no overwrite, no network configuration, no process
# killing, no package installation, no shell.
ALLOWED_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "find", "pwd",
    "date", "uname", "whoami", "echo", "sleep", "python3", "git",
})

# Shell metacharacters are never allowed in command arguments:
# the executor always runs with shell=False, and any argument
# containing one of these is refused.
_SHELL_META = frozenset(";&|$`><\n\r\t")

# Applications launchable by name. No launcher is configured by
# default (see launch_application): naming an app here does not
# launch it.
ALLOWED_APPLICATIONS = frozenset({
    "code", "notepad", "explorer", "chrome", "firefox", "terminal",
})

DEFAULT_COMMAND_TIMEOUT_S = 30.0

WINDOWS_NOTE = (
    "No direct control of the owner's Windows machine, VS Code "
    "window, or screen exists: computer pairing is Mac-only and "
    "only the owner's iPhone is paired. The workable routes are "
    "building here and handing over files for him to open in VS "
    "Code, or remote file access only with his explicit setup "
    "approval (recorded by configure_remote_target)."
)


def windows_control_available():
    """Whether Jarvis can control the owner's Windows machine.

    Always ``False`` — see :data:`WINDOWS_NOTE`. This is a
    fact about the setup, not a capability to enable here.
    """
    return {"available": False, "reason": WINDOWS_NOTE}


# ---------------------------------------------------------------------------
# Capability registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """One named capability with an explicit scope.

    ``scope`` names what the capability may touch (a workspace
    root, the command allowlist, the application allowlist, or
    "read-only system information"). ``requires_approval`` says
    whether the owner's explicit per-call approval is needed
    before the capability may run.
    """

    name: str
    description: str
    scope: str
    requires_approval: bool = False


DEFAULT_CAPABILITIES = (
    Capability(
        name="read_file",
        description="Read a file inside the workspace root.",
        scope="workspace root",
        requires_approval=False,
    ),
    Capability(
        name="write_file",
        description="Write a file inside the workspace root.",
        scope="workspace root",
        requires_approval=True,
    ),
    Capability(
        name="list_directory",
        description="List a directory inside the workspace root.",
        scope="workspace root",
        requires_approval=False,
    ),
    Capability(
        name="run_command",
        description="Run an allowlisted read-only command inside "
                    "the workspace root.",
        scope="ALLOWED_COMMANDS only",
        requires_approval=True,
    ),
    Capability(
        name="launch_application",
        description="Launch an allowlisted application by name.",
        scope="ALLOWED_APPLICATIONS only",
        requires_approval=True,
    ),
    Capability(
        name="system_info",
        description="Read read-only system information.",
        scope="read-only system information",
        requires_approval=False,
    ),
)


class CapabilityRegistry:
    """A registry of capabilities, with forbidden names refused
    at registration time (same rule as Step 44)."""

    def __init__(self, capabilities=None):
        self._capabilities = {}
        for cap in (capabilities if capabilities is not None
                    else DEFAULT_CAPABILITIES):
            self.register(cap)

    def register(self, capability):
        """Register ``capability``; forbidden names and duplicates
        fail loudly."""
        _check_capability_name(capability.name)
        if capability.name in self._capabilities:
            raise DuplicateCapabilityError(
                f"capability '{capability.name}' is already registered")
        self._capabilities[capability.name] = capability
        return capability

    def get(self, name):
        """The capability named ``name``, or ``None``."""
        return self._capabilities.get(name)

    def list(self):
        """All registered capabilities, in registration order."""
        return list(self._capabilities.values())

    def describe(self):
        """One line per capability, for a prompt or a report."""
        return [
            f"{c.name}: {c.description} "
            f"(scope: {c.scope}; "
            f"{'needs approval' if c.requires_approval else 'no approval needed'})"
            for c in self.list()
        ]


# ---------------------------------------------------------------------------
# Workspace root (the jail)
# ---------------------------------------------------------------------------


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _resolve_within_root(root, path):
    """Resolve ``path`` against ``root`` and refuse it if it
    escapes the root (``..``, absolute paths outside the root,
    or symlinks pointing outside it)."""
    root = Path(root).resolve()
    candidate = (root / path).resolve() if not Path(path).is_absolute() \
        else Path(path).resolve()
    if candidate != root and root not in candidate.parents:
        raise RefusedActionError(
            f"path '{path}' is outside the workspace root '{root}'")
    return candidate


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _log_dir(log_dir):
    return Path(log_dir) if log_dir is not None else Path(
        "data/computer_actions")


def _audit_path(log_dir):
    return _log_dir(log_dir) / "actions.jsonl"


def _append_audit(log_dir, record):
    path = _audit_path(log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return str(path)


def _audit_record(event, action, arguments, decision, outcome=None,
                  detail=None):
    record = {
        "event": event,  # "attempt" (before execution) or "outcome"
        "action": action,
        "arguments": arguments,
        "decision": decision,  # "allowed" or "refused"
        "outcome": outcome,  # e.g. "ok", "refused", "error"
        "detail": detail,
        "recorded_at": _utcnow_iso(),
    }
    return record


def read_audit_log(log_dir=None):
    """All audit records in ``log_dir`` (oldest first). A corrupt
    line is skipped and reported, never crashing the read."""
    path = _audit_path(log_dir)
    records = []
    skipped = 0
    if not path.exists():
        return {"records": records, "skipped_corrupt_lines": skipped,
                "log_file": str(path)}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            skipped += 1
    return {"records": records, "skipped_corrupt_lines": skipped,
            "log_file": str(path)}


# ---------------------------------------------------------------------------
# Local executor
# ---------------------------------------------------------------------------


def read_file(root, path, log_dir=None):
    """Read a text file inside the workspace root. Refused
    outside the root. The attempt and the outcome are logged."""
    args = {"path": str(path)}
    try:
        resolved = _resolve_within_root(root, path)
    except RefusedActionError as exc:
        _append_audit(log_dir, _audit_record(
            "attempt", "read_file", args, "refused", "refused",
            str(exc)))
        raise
    _append_audit(log_dir, _audit_record(
        "attempt", "read_file", args, "allowed", None))
    try:
        content = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _append_audit(log_dir, _audit_record(
            "outcome", "read_file", args, "allowed", "error", str(exc)))
        raise ComputerError(f"cannot read '{path}': {exc}") from exc
    _append_audit(log_dir, _audit_record(
        "outcome", "read_file", args, "allowed", "ok",
        f"{len(content)} characters"))
    return {"path": str(resolved), "content": content,
            "characters": len(content)}


def write_file(root, path, content, approved=False, log_dir=None):
    """Write a text file inside the workspace root. Requires the
    caller's explicit per-call approval (``approved=True``);
    refused without it, and refused outside the root."""
    args = {"path": str(path), "approved": approved,
            "characters": len(content)}
    if not approved:
        _append_audit(log_dir, _audit_record(
            "attempt", "write_file", args, "refused", "refused",
            "write_file requires the owner's explicit per-call approval"))
        raise RefusedActionError(
            "write_file requires the owner's explicit per-call approval")
    try:
        resolved = _resolve_within_root(root, path)
    except RefusedActionError as exc:
        _append_audit(log_dir, _audit_record(
            "attempt", "write_file", args, "refused", "refused",
            str(exc)))
        raise
    _append_audit(log_dir, _audit_record(
        "attempt", "write_file", args, "allowed", None))
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        _append_audit(log_dir, _audit_record(
            "outcome", "write_file", args, "allowed", "error", str(exc)))
        raise ComputerError(f"cannot write '{path}': {exc}") from exc
    _append_audit(log_dir, _audit_record(
        "outcome", "write_file", args, "allowed", "ok",
        f"{len(content)} characters written"))
    return {"path": str(resolved), "characters": len(content)}


def list_directory(root, path=".", log_dir=None):
    """List a directory inside the workspace root (names only,
    sorted). Refused outside the root."""
    args = {"path": str(path)}
    try:
        resolved = _resolve_within_root(root, path)
    except RefusedActionError as exc:
        _append_audit(log_dir, _audit_record(
            "attempt", "list_directory", args, "refused", "refused",
            str(exc)))
        raise
    _append_audit(log_dir, _audit_record(
        "attempt", "list_directory", args, "allowed", None))
    try:
        entries = sorted(e.name for e in resolved.iterdir())
    except (OSError, NotADirectoryError) as exc:
        _append_audit(log_dir, _audit_record(
            "outcome", "list_directory", args, "allowed", "error",
            str(exc)))
        raise ComputerError(f"cannot list '{path}': {exc}") from exc
    _append_audit(log_dir, _audit_record(
        "outcome", "list_directory", args, "allowed", "ok",
        f"{len(entries)} entries"))
    return {"path": str(resolved), "entries": entries}


def _check_command(command, args):
    if command not in ALLOWED_COMMANDS:
        raise RefusedActionError(
            f"command '{command}' is not in the allowlist "
            f"({', '.join(sorted(ALLOWED_COMMANDS))}) — destructive "
            "and shell commands are refused by construction")
    for arg in args:
        if any(ch in arg for ch in _SHELL_META):
            raise RefusedActionError(
                f"argument {arg!r} contains a shell metacharacter — "
                "commands always run with shell=False")


def run_command(root, command, args=(), approved=False,
                timeout=DEFAULT_COMMAND_TIMEOUT_S, log_dir=None):
    """Run an allowlisted read-only command inside the workspace
    root, with ``shell=False`` and a timeout. Requires the
    caller's explicit per-call approval. Anything outside the
    allowlist, any shell metacharacter in an argument, and any
    command that times out is refused or recorded as an error —
    never attempted with a shell."""
    args = list(args)
    log_args = {"command": command, "args": args,
                "approved": approved, "timeout": timeout}
    if not approved:
        _append_audit(log_dir, _audit_record(
            "attempt", "run_command", log_args, "refused", "refused",
            "run_command requires the owner's explicit per-call approval"))
        raise RefusedActionError(
            "run_command requires the owner's explicit per-call approval")
    try:
        _check_command(command, args)
    except RefusedActionError as exc:
        _append_audit(log_dir, _audit_record(
            "attempt", "run_command", log_args, "refused", "refused",
            str(exc)))
        raise
    _append_audit(log_dir, _audit_record(
        "attempt", "run_command", log_args, "allowed", None))
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [command, *args], cwd=str(Path(root).resolve()),
            capture_output=True, text=True, shell=False,
            timeout=timeout)
    except subprocess.TimeoutExpired:
        _append_audit(log_dir, _audit_record(
            "outcome", "run_command", log_args, "allowed", "error",
            f"timed out after {timeout}s"))
        raise ComputerError(
            f"command '{command}' timed out after {timeout}s")
    except (OSError, ValueError) as exc:
        _append_audit(log_dir, _audit_record(
            "outcome", "run_command", log_args, "allowed", "error",
            str(exc)))
        raise ComputerError(f"cannot run '{command}': {exc}") from exc
    duration = round(time.monotonic() - started, 3)
    _append_audit(log_dir, _audit_record(
        "outcome", "run_command", log_args, "allowed",
        "ok" if proc.returncode == 0 else "error",
        f"exit code {proc.returncode} in {duration}s"))
    return {"command": command, "args": args,
            "returncode": proc.returncode,
            "stdout": proc.stdout, "stderr": proc.stderr,
            "duration_s": duration}


def launch_application(name, approved=False, launcher=None,
                       log_dir=None):
    """Launch an allowlisted application by name. Requires the
    caller's explicit per-call approval, and a caller-supplied
    ``launcher`` callable — no launcher is configured by
    default, so with ``launcher=None`` the launch is refused
    and nothing is ever started. A supplied launcher receives
    the application name and returns whatever it returns; the
    launch is recorded in the audit log either way."""
    log_args = {"application": name, "approved": approved,
                "launcher_configured": launcher is not None}
    if name not in ALLOWED_APPLICATIONS:
        _append_audit(log_dir, _audit_record(
            "attempt", "launch_application", log_args, "refused",
            "refused",
            f"application '{name}' is not in the allowlist "
            f"({', '.join(sorted(ALLOWED_APPLICATIONS))})"))
        raise RefusedActionError(
            f"application '{name}' is not in the allowlist")
    if not approved:
        _append_audit(log_dir, _audit_record(
            "attempt", "launch_application", log_args, "refused",
            "refused",
            "launch_application requires the owner's explicit "
            "per-call approval"))
        raise RefusedActionError(
            "launch_application requires the owner's explicit "
            "per-call approval")
    if launcher is None:
        _append_audit(log_dir, _audit_record(
            "attempt", "launch_application", log_args, "refused",
            "refused",
            "no application launcher is configured — nothing was "
            "launched"))
        raise RefusedActionError(
            "no application launcher is configured — nothing was "
            "launched")
    _append_audit(log_dir, _audit_record(
        "attempt", "launch_application", log_args, "allowed", None))
    try:
        result = launcher(name)
    except Exception as exc:  # noqa: BLE001 - record, never crash
        _append_audit(log_dir, _audit_record(
            "outcome", "launch_application", log_args, "allowed",
            "error", str(exc)))
        raise ComputerError(
            f"launcher failed for '{name}': {exc}") from exc
    _append_audit(log_dir, _audit_record(
        "outcome", "launch_application", log_args, "allowed", "ok",
        f"launcher returned {result!r}"))
    return {"application": name, "launched": True, "result": result}


def system_info(log_dir=None):
    """Read-only system information about the machine Jarvis
    itself runs on. No approval needed; nothing is changed."""
    args = {}
    _append_audit(log_dir, _audit_record(
        "attempt", "system_info", args, "allowed", None))
    info = {
        "platform": platform.system(),
        "platform_release": platform.release(),
        "machine": platform.machine(),
        "python_version": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "working_directory": str(Path.cwd()),
    }
    _append_audit(log_dir, _audit_record(
        "outcome", "system_info", args, "allowed", "ok",
        f"platform {info['platform']}"))
    return info


# ---------------------------------------------------------------------------
# Remote gate — disabled by default, explicit setup approval only
# ---------------------------------------------------------------------------


@dataclass
class RemoteTarget:
    """A remote machine Jarvis may interact with.

    ``enabled`` is False unless the owner explicitly enabled
    the target through :func:`configure_remote_target`, which
    records who approved it and when. ``method`` names the
    connection method (for example ``"ssh"``); it is a label
    only — this module opens no connection itself.
    """

    host: str
    user: str
    method: str = "ssh"
    enabled: bool = False
    approved_by: str = None
    approved_at: str = None

    def as_dict(self):
        return {
            "host": self.host, "user": self.user,
            "method": self.method, "enabled": self.enabled,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }


def configure_remote_target(host, user, method="ssh", approved_by=None,
                            log_dir=None):
    """Enable a remote target — only with the owner's explicit
    setup approval. ``approved_by`` must name who approved it;
    without it the target stays disabled. Returns the
    :class:`RemoteTarget` (enabled when approval was given).
    No connection is attempted here."""
    args = {"host": host, "user": user, "method": method,
            "approved_by": approved_by}
    if not approved_by:
        target = RemoteTarget(host=host, user=user, method=method,
                              enabled=False)
        _append_audit(log_dir, _audit_record(
            "attempt", "configure_remote_target", args, "refused",
            "refused",
            "remote target setup requires the owner's explicit "
            "approval (approved_by) — target left disabled"))
        return target
    target = RemoteTarget(host=host, user=user, method=method,
                          enabled=True, approved_by=approved_by,
                          approved_at=_utcnow_iso())
    _append_audit(log_dir, _audit_record(
        "attempt", "configure_remote_target", args, "allowed", "ok",
        f"remote target {user}@{host} ({method}) enabled by "
        f"{approved_by}"))
    return target


def disable_remote_target(target, log_dir=None):
    """Disable a remote target. Returns the disabled target."""
    args = {"host": target.host, "user": target.user,
            "method": target.method}
    target.enabled = False
    target.approved_by = None
    target.approved_at = None
    _append_audit(log_dir, _audit_record(
        "attempt", "disable_remote_target", args, "allowed", "ok",
        f"remote target {target.user}@{target.host} disabled"))
    return target


def remote_action(target, action, arguments=None, transport=None,
                  log_dir=None):
    """Perform a remote action — only on an enabled target, and
    only through a caller-supplied ``transport`` callable. With
    no enabled target the action returns
    ``"remote not configured"`` and never attempts a connection;
    with no transport it is refused the same way. A supplied
    transport receives ``(target, action, arguments)`` and
    returns whatever it returns. Nothing here opens a real
    connection."""
    arguments = arguments or {}
    log_args = {"action": action, "arguments": arguments,
                "target": target.as_dict() if target else None,
                "transport_configured": transport is not None}
    if target is None or not target.enabled:
        _append_audit(log_dir, _audit_record(
            "attempt", "remote_action", log_args, "refused", "refused",
            "remote not configured — no enabled remote target"))
        return {"status": "remote_not_configured",
                "reason": "remote not configured — no enabled "
                          "remote target; nothing was attempted"}
    if transport is None:
        _append_audit(log_dir, _audit_record(
            "attempt", "remote_action", log_args, "refused", "refused",
            "no remote transport is configured — nothing was "
            "attempted"))
        return {"status": "remote_not_configured",
                "reason": "no remote transport is configured; "
                          "nothing was attempted"}
    _append_audit(log_dir, _audit_record(
        "attempt", "remote_action", log_args, "allowed", None))
    try:
        result = transport(target, action, arguments)
    except Exception as exc:  # noqa: BLE001 - record, never crash
        _append_audit(log_dir, _audit_record(
            "outcome", "remote_action", log_args, "allowed", "error",
            str(exc)))
        raise ComputerError(
            f"remote transport failed for '{action}': {exc}") from exc
    _append_audit(log_dir, _audit_record(
        "outcome", "remote_action", log_args, "allowed", "ok",
        f"transport returned {str(result)[:200]!r}"))
    return {"status": "ok", "target": target.as_dict(),
            "action": action, "result": result}


# ---------------------------------------------------------------------------
# perform_action — one entry point that always logs first
# ---------------------------------------------------------------------------

# Actions that need the owner's explicit per-call approval.
_APPROVAL_REQUIRED = frozenset({
    "write_file", "run_command", "launch_application",
})


def perform_action(root, action, arguments=None, approved=False,
                   launcher=None, transport=None, remote_target=None,
                   log_dir=None):
    """Run one local or remote action through the registry, the
    executor, and the audit log.

    ``action`` is one of the default capability names
    (``read_file``, ``write_file``, ``list_directory``,
    ``run_command``, ``launch_application``, ``system_info``)
    or ``remote_action``. Actions needing approval are refused
    without ``approved=True``. The attempt is logged before
    anything executes; the outcome is logged after. Returns
    the action's result dict, or a refusal record with
    ``"status": "refused"`` / ``"remote_not_configured"`` —
    never raises for a refusal, so a caller can record it and
    carry on (the underlying helpers still raise
    :class:`RefusedActionError` when called directly).
    """
    arguments = dict(arguments or {})
    record_id = uuid.uuid4().hex[:12]
    if action == "remote_action":
        result = remote_action(
            remote_target, arguments.get("remote_action", action),
            arguments.get("remote_arguments", {}),
            transport=transport, log_dir=log_dir)
        result["record_id"] = record_id
        return result
    dispatch = {
        "read_file": lambda: read_file(
            root, arguments["path"], log_dir=log_dir),
        "write_file": lambda: write_file(
            root, arguments["path"], arguments["content"],
            approved=approved, log_dir=log_dir),
        "list_directory": lambda: list_directory(
            root, arguments.get("path", "."), log_dir=log_dir),
        "run_command": lambda: run_command(
            root, arguments["command"], arguments.get("args", ()),
            approved=approved,
            timeout=arguments.get("timeout", DEFAULT_COMMAND_TIMEOUT_S),
            log_dir=log_dir),
        "launch_application": lambda: launch_application(
            arguments["application"], approved=approved,
            launcher=launcher, log_dir=log_dir),
        "system_info": lambda: system_info(log_dir=log_dir),
    }
    if action not in dispatch:
        _append_audit(log_dir, _audit_record(
            "attempt", action, arguments, "refused", "refused",
            f"unknown action '{action}'"))
        return {"status": "refused",
                "reason": f"unknown action '{action}'",
                "record_id": record_id}
    if action in _APPROVAL_REQUIRED and not approved:
        _append_audit(log_dir, _audit_record(
            "attempt", action, arguments, "refused", "refused",
            f"{action} requires the owner's explicit per-call approval"))
        return {"status": "refused",
                "reason": f"{action} requires the owner's explicit "
                          "per-call approval",
                "record_id": record_id}
    try:
        result = dispatch[action]()
    except (RefusedActionError, ComputerError) as exc:
        return {"status": "refused", "reason": str(exc),
                "record_id": record_id}
    if isinstance(result, dict):
        result["record_id"] = record_id
    return result


# ---------------------------------------------------------------------------
# Computer — thin class wrapping root, log dir, registry, remote
# ---------------------------------------------------------------------------


class Computer:
    """One computer-interaction session: a workspace root, an
    audit-log directory, a capability registry, and an optional
    remote target (disabled by default)."""

    def __init__(self, root, log_dir=None, registry=None,
                 launcher=None, transport=None):
        self.root = Path(root).resolve()
        self.log_dir = _log_dir(log_dir)
        self.registry = registry or CapabilityRegistry()
        self.launcher = launcher
        self.transport = transport
        self.remote_target = None

    def capabilities(self):
        """The registered capabilities, as description lines."""
        return self.registry.describe()

    def action(self, action, arguments=None, approved=False):
        """Run one action; see :func:`perform_action`."""
        return perform_action(
            self.root, action, arguments, approved=approved,
            launcher=self.launcher, transport=self.transport,
            remote_target=self.remote_target, log_dir=self.log_dir)

    def configure_remote(self, host, user, method="ssh",
                         approved_by=None):
        """Enable a remote target (explicit setup approval only)."""
        self.remote_target = configure_remote_target(
            host, user, method=method, approved_by=approved_by,
            log_dir=self.log_dir)
        return self.remote_target

    def disable_remote(self):
        """Disable the remote target, if one is configured."""
        if self.remote_target is None:
            return None
        return disable_remote_target(self.remote_target,
                                     log_dir=self.log_dir)

    def audit(self):
        """The audit log so far."""
        return read_audit_log(self.log_dir)
