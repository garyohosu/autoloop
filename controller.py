"""Lightweight controller for iterative AI-assisted development.

AutoLoop runs a configured coding agent inside a target Git repository, executes
fixed verification commands, and stores receipts and logs in that target
repository. It never commits or pushes changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DESIGN_FILES = [
    "SPEC.md",
    "USECASE.md",
    "SEQUENCE.md",
    "CLASS.md",
    "UI.md",
    "TESTCASE.md",
    "QandA.md",
]
DEFAULT_AGENT_COMMAND = ["codex", "exec", "--sandbox", "workspace-write"]
VALID_PROMPT_DELIVERIES = frozenset({"argument", "stdin"})
AUTOLOOP_CONFIG_DIR = ".autoloop"


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class ControllerError(RuntimeError):
    """Raised when AutoLoop cannot safely continue."""


def repo_root(start: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ControllerError(f"Git repository lookup failed: {exc}") from exc
    if result.returncode:
        raise ControllerError(result.stderr.strip() or "Git repository not found")
    return Path(result.stdout.strip()).resolve()


def resolve_project_root(project: Path | None, cwd: Path | None = None) -> Path:
    """Resolve --project, or the current directory, to a Git repository root."""
    start = (project if project is not None else (cwd or Path.cwd())).expanduser()
    if not start.exists():
        raise ControllerError(f"project does not exist: {start}")
    if not start.is_dir():
        raise ControllerError(f"project must be a directory: {start}")
    return repo_root(start.resolve())


def resolve_config_path(config: Path | None, root: Path) -> Path:
    """Resolve a relative configuration path against the target repository."""
    value = config or Path(".autoloop/config.json")
    return value if value.is_absolute() else root / value


def relative(value: str) -> str:
    path = value.replace("\\", "/")
    if not path or path.startswith("/") or Path(path).drive or ".." in path.split("/"):
        raise ControllerError(f"relative path required: {value}")
    return path


@dataclass
class Config:
    root: Path
    automation_dir: str = ".runtime"
    design_files: list[str] = field(default_factory=lambda: DEFAULT_DESIGN_FILES.copy())
    verification_commands: list[list[str]] = field(default_factory=list)
    agent_name: str = "codex"
    agent_command: list[str] = field(default_factory=lambda: DEFAULT_AGENT_COMMAND.copy())
    prompt_delivery: str = "argument"
    agent_timeout_seconds: int = 1800
    max_cycles: int = 10
    stop_on_agent_failure: bool = True
    stop_on_test_failure: bool = False
    commit_enabled: bool = False
    push_enabled: bool = False
    allow_dirty_worktree: bool = False
    allowed_dirty_paths: list[str] = field(default_factory=list)
    # Single-task gate (opt-in; default allow_task_chaining=True means this
    # is off). Existing configs that omit this key keep the original "agent
    # picks any task" behaviour unchanged. Set allow_task_chaining=false to
    # restrict AutoLoop to the one task_id named in task_file, stopping once
    # its status leaves the actionable set ("pending"/"in_progress").
    allow_task_chaining: bool = True
    task_file: str = "instructions/instructions.md"

    @classmethod
    def load(cls, path: Path, root: Path | None = None) -> "Config":
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControllerError(f"config load failed: {exc}") from exc
        if not isinstance(raw, dict):
            raise ControllerError("config must be an object")

        detected = (root or repo_root(Path.cwd())).resolve()
        configured = raw.get("repo_path")
        if configured and Path(configured).expanduser().resolve() != detected:
            raise ControllerError("repo_path does not match target Git root")

        agent = raw.get("agent", {})
        if not isinstance(agent, dict):
            raise ControllerError("agent must be an object")
        command = agent.get("command", DEFAULT_AGENT_COMMAND.copy())
        commands = raw.get(
            "verification_commands",
            raw.get("verification", {}).get("commands", []),
        )
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise ControllerError("agent.command must be a non-empty argv list")
        if (
            not isinstance(commands, list)
            or any(
                not isinstance(item, list)
                or not item
                or any(not isinstance(argument, str) or not argument for argument in item)
                for item in commands
            )
        ):
            raise ControllerError("verification_commands must be argv lists")

        files = [relative(str(item)) for item in raw.get("design_files", DEFAULT_DESIGN_FILES)]
        automation = relative(str(raw.get("automation_dir", ".runtime")))
        prompt_delivery = str(agent.get("prompt_delivery", "argument"))
        if prompt_delivery not in VALID_PROMPT_DELIVERIES:
            raise ControllerError(
                "agent.prompt_delivery must be one of "
                f"{sorted(VALID_PROMPT_DELIVERIES)}: {prompt_delivery!r}"
            )

        allowed_raw = raw.get("allowed_dirty_paths", [])
        if not isinstance(allowed_raw, list) or any(
            not isinstance(item, str) for item in allowed_raw
        ):
            raise ControllerError("allowed_dirty_paths must be a list of strings")

        return cls(
            detected,
            automation,
            files,
            commands,
            str(agent.get("name", "codex")),
            command,
            prompt_delivery,
            int(agent.get("timeout_seconds", 1800)),
            int(raw.get("max_cycles", 10)),
            bool(raw.get("stop_on_agent_failure", True)),
            bool(raw.get("stop_on_test_failure", False)),
            bool(raw.get("commit_enabled", False)),
            bool(raw.get("push_enabled", False)),
            bool(raw.get("allow_dirty_worktree", False)),
            [relative(item) for item in allowed_raw],
            allow_task_chaining=bool(raw.get("allow_task_chaining", True)),
            task_file=relative(str(raw.get("task_file", "instructions/instructions.md"))),
        )


LOCK_SCHEMA_VERSION = 1
_REQUIRED_LOCK_FIELDS = (
    "schema_version", "repository", "pid", "started_at", "hostname",
    "controller_instance_id", "mode",
)


def _normalize_repository_path(root: Path) -> str:
    """Canonical identifier for a Git repository root.

    Used as the lock's `repository` field so that the same repository
    invoked via a relative path, a trailing separator, a different drive
    letter case, or a resolvable symlink still maps to one shared lock.
    """
    resolved = root.resolve()
    text = str(resolved)
    if os.name == "nt":
        text = text.rstrip("\\/")
        if len(text) >= 2 and text[1] == ":":
            text = text[0].upper() + text[1:]
    else:
        text = text.rstrip("/")
    return text


def _windows_process_start_time(pid: int) -> str | None:
    """Return an opaque, comparable process-start token, or None if the pid
    does not currently exist. Windows-only; uses ctypes (stdlib) against
    kernel32, no third-party dependency.
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return ""
        value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return str(value)
    finally:
        kernel32.CloseHandle(handle)


def _pid_liveness(pid: int, expected_start: str | None) -> str:
    """Classify a recorded pid as "alive", "dead", or "unknown".

    A pid that exists but whose process-start token does not match the one
    recorded in the lock is treated as "dead": the original process is gone
    and the OS has reused the pid number for something else.
    """
    if os.name == "nt":
        try:
            current_start = _windows_process_start_time(pid)
        except Exception:
            return "unknown"
        if current_start is None:
            return "dead"
        if expected_start and current_start and current_start != expected_start:
            return "dead"
        return "alive"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        return "unknown"
    return "alive"


@dataclass
class LockRecord:
    """The persisted contents of `.autoloop/run.lock`."""

    schema_version: int
    repository: str
    pid: int
    started_at: str
    hostname: str
    controller_instance_id: str
    mode: str
    parent_pid: int | None = None
    process_started_at: str | None = None
    controller_path: str | None = None
    cycle: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "pid": self.pid,
            "parent_pid": self.parent_pid,
            "started_at": self.started_at,
            "process_started_at": self.process_started_at,
            "hostname": self.hostname,
            "controller_instance_id": self.controller_instance_id,
            "controller_path": self.controller_path,
            "mode": self.mode,
            "cycle": self.cycle,
        }


@dataclass
class LockStatus:
    """Read-only classification of the current lock file.

    state is one of: "none", "active", "stale", "foreign_host", "invalid",
    "unknown". `record` is populated whenever a lock file could be parsed,
    even for "invalid" (e.g. a repository mismatch) where the record itself
    parsed fine but failed a cross-check.
    """

    state: str
    record: LockRecord | None = None
    error: str | None = None


class LockAcquisitionError(ControllerError):
    """Raised by RepositoryLock.acquire() when the lock is not available."""

    def __init__(self, status: LockStatus) -> None:
        self.status = status
        super().__init__(f"repository lock not acquired: {status.state}")


class RepositoryLock:
    """Exclusive, per-repository lock at `.autoloop/run.lock`.

    Replaces the earlier `.runtime/autoloop.lock` Lock class, which recorded
    only pid/started_at and never checked whether that pid was still alive:
    a crashed run left a lock that blocked every future run until a human
    deleted the file by hand, with no way to tell "still running" apart from
    "crashed" from outside the process. This class adds pid-liveness (with
    process-start-time comparison to guard against pid reuse), hostname
    scoping, and an explicit stale/foreign_host/invalid/unknown
    classification so a stuck lock can be diagnosed and safely cleared.
    """

    def __init__(
        self,
        root: Path,
        mode: str = "chaining",
        controller_path: str | None = None,
    ) -> None:
        self.root = root
        self.path = root / AUTOLOOP_CONFIG_DIR / "run.lock"
        self.repository = _normalize_repository_path(root)
        self.mode = mode
        self.controller_path = controller_path
        self.instance_id = str(uuid.uuid4())
        self.held = False
        self._last_started_at: str | None = None

    def _new_record(self) -> LockRecord:
        return LockRecord(
            schema_version=LOCK_SCHEMA_VERSION,
            repository=self.repository,
            pid=os.getpid(),
            parent_pid=os.getppid() if hasattr(os, "getppid") else None,
            started_at=timestamp(),
            process_started_at=(
                _windows_process_start_time(os.getpid()) if os.name == "nt" else None
            ),
            hostname=socket.gethostname(),
            controller_instance_id=self.instance_id,
            controller_path=self.controller_path,
            mode=self.mode,
        )

    def _parse(self) -> LockStatus:
        if not self.path.is_file():
            return LockStatus(state="none")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return LockStatus(state="invalid", error="lock file is not valid JSON")
        if not isinstance(raw, dict):
            return LockStatus(state="invalid", error="lock file is not a JSON object")
        if any(key not in raw for key in _REQUIRED_LOCK_FIELDS):
            return LockStatus(state="invalid", error="lock file missing required fields")
        try:
            record = LockRecord(
                schema_version=int(raw["schema_version"]),
                repository=str(raw["repository"]),
                pid=int(raw["pid"]),
                started_at=str(raw["started_at"]),
                hostname=str(raw["hostname"]),
                controller_instance_id=str(raw["controller_instance_id"]),
                mode=str(raw["mode"]),
                parent_pid=raw.get("parent_pid"),
                process_started_at=raw.get("process_started_at"),
                controller_path=raw.get("controller_path"),
                cycle=raw.get("cycle"),
            )
        except (TypeError, ValueError):
            return LockStatus(state="invalid", error="lock file has a field of the wrong type")
        if record.repository != self.repository:
            return LockStatus(
                state="invalid", record=record,
                error="lock file repository does not match this repository",
            )
        return LockStatus(state="unresolved", record=record)

    def status(self) -> LockStatus:
        """Classify the current lock file without acquiring or modifying it."""
        parsed = self._parse()
        if parsed.state != "unresolved":
            return parsed
        record = parsed.record
        if record.hostname.lower() != socket.gethostname().lower():
            # Windows hostnames are case-insensitive (e.g. $env:COMPUTERNAME
            # vs socket.gethostname() can differ only in case for the same
            # machine); compare case-insensitively so that doesn't produce a
            # false foreign_host. A genuinely different host cannot be
            # pid-checked at all from here, so it is still treated the same
            # as a live lock rather than guessing.
            return LockStatus(state="foreign_host", record=record)
        liveness = _pid_liveness(record.pid, record.process_started_at)
        if liveness == "alive":
            return LockStatus(state="active", record=record)
        if liveness == "dead":
            return LockStatus(state="stale", record=record)
        return LockStatus(state="unknown", record=record)

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = self._new_record()
        try:
            with self.path.open("x", encoding="utf-8") as stream:
                json.dump(record.to_dict(), stream, ensure_ascii=False, indent=2)
        except FileExistsError:
            raise LockAcquisitionError(self.status()) from None
        self.held = True
        self._last_started_at = record.started_at

    def release(self) -> tuple[bool, str | None]:
        """Release the lock. Returns (succeeded, warning).

        Only removes the file if it still records this instance's
        controller_instance_id, so a lock some other process re-created
        (e.g. after this one crashed and a human force-cleared it) is never
        deleted out from under that other process.
        """
        if not self.held:
            return True, None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            self.held = False
            return False, "lock release skipped: lock file unreadable or not valid JSON"
        if not isinstance(raw, dict) or raw.get("controller_instance_id") != self.instance_id:
            self.held = False
            return False, "lock release skipped: lock file no longer owned by this instance"
        try:
            self.path.unlink()
        except OSError as exc:
            return False, f"lock release failed: {exc}"
        self.held = False
        return True, None

    def unlock_stale(self) -> LockStatus:
        """Delete the lock file only when status() says it is unambiguously
        stale. Never deletes active/foreign_host/invalid/unknown/none.
        """
        current = self.status()
        if current.state == "stale":
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        return current


def safe_lock_status(status: LockStatus) -> dict[str, Any]:
    """Allowlisted view of a LockStatus for CLI display and receipts.

    Deliberately omits parent_pid, process_started_at, and controller_path:
    section 11's -LockStatus contract lists only state/pid/started_at/
    repository/hostname/mode, and none of command, username, or environment
    are ever included.
    """
    result: dict[str, Any] = {"state": status.state}
    if status.record is not None:
        result.update(
            pid=status.record.pid,
            started_at=status.record.started_at,
            repository=status.record.repository,
            hostname=status.record.hostname,
            mode=status.record.mode,
        )
    if status.error:
        result["error"] = status.error
    return result


_TASK_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TASK_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]*(.*?)[ \t]*$", re.MULTILINE)

# Status values the gate file's front matter may declare. "pending" and
# "in_progress" both authorize an agent cycle; "completed"/"blocked"/"failed"
# are recognized but stop the loop without spending an agent call.
TASK_GATE_STATUSES = frozenset({"pending", "in_progress", "completed", "blocked", "failed"})
ACTIONABLE_TASK_STATUSES = frozenset({"pending", "in_progress"})


@dataclass
class TaskGateState:
    """Result of parsing the single-task gate file's front matter.

    valid=False covers every fail-closed case in the gate contract
    (missing file, unreadable, malformed front matter, missing/empty
    task_id or status, unrecognized status), so callers never have to
    guess at the reason from a bare None. task_id/status may still be
    set on some invalid results (e.g. an unrecognized status) for receipt
    diagnostics; error is always a short, safe (no raw file body) reason
    whenever valid is False.
    """

    valid: bool
    task_id: str | None = None
    status: str | None = None
    error: str | None = None


def read_task_state(root: Path, task_file: str) -> TaskGateState:
    """Parse the task_id/status YAML-style frontmatter of task_file."""
    path = root / task_file
    if not path.is_file():
        return TaskGateState(valid=False, error="task_file not found")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return TaskGateState(valid=False, error="task_file could not be read")
    match = _TASK_FRONTMATTER_RE.match(text)
    if not match:
        return TaskGateState(valid=False, error="task_file front matter missing or malformed")
    fields = dict(_TASK_FIELD_RE.findall(match.group(1)))
    task_id = fields.get("task_id", "").strip()
    status = fields.get("status", "").strip()
    if not task_id:
        return TaskGateState(valid=False, error="task_id missing or empty")
    if not status:
        return TaskGateState(valid=False, task_id=task_id, error="status missing or empty")
    if status not in TASK_GATE_STATUSES:
        return TaskGateState(
            valid=False,
            task_id=task_id,
            status=status,
            error=f"status not recognized: {status!r}",
        )
    return TaskGateState(valid=True, task_id=task_id, status=status)


def _clean_status_path(value: str) -> str:
    return value.strip().strip('"').replace("\\", "/")


def status_entries(
    root: Path, exclude: str | tuple[str, ...] | None = None
) -> list[dict[str, str | None]]:
    """Parse `git status --porcelain -uall`, optionally excluding directories."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ControllerError(result.stderr.strip() or "git status failed")
    excludes = (exclude,) if isinstance(exclude, str) else tuple(exclude or ())
    prefixes = tuple(f"{item}/" for item in excludes)
    entries: list[dict[str, str | None]] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        status, value = line[:2], line[3:]
        orig = None
        if " -> " in value:
            orig, value = value.split(" -> ", 1)
        path = _clean_status_path(value)
        if excludes and (path in excludes or path.startswith(prefixes)):
            continue
        entries.append(
            {
                "status": status,
                "path": path,
                "orig_path": _clean_status_path(orig) if orig else None,
            }
        )
    return entries


def changed_files(root: Path, exclude: str | tuple[str, ...] | None = None) -> list[str]:
    return sorted({entry["path"] for entry in status_entries(root, exclude)})


def file_record(root: Path, path: str, status: str | None, orig_path: str | None) -> dict[str, Any]:
    """Capture the observable state of one path for dirty-file protection."""
    target = root / path
    kind, size, digest = "missing", None, None
    if target.is_symlink():
        kind = "symlink"
    elif target.is_dir():
        kind = "dir"
    elif target.is_file():
        data = target.read_bytes()
        kind, size, digest = "file", len(data), hashlib.sha256(data).hexdigest()
    return {
        "status": status,
        "kind": kind,
        "exists": kind != "missing",
        "size": size,
        "sha256": digest,
        "orig_path": orig_path,
    }


def snapshot(
    root: Path, paths: list[str], exclude: str | tuple[str, ...] | None = None
) -> dict[str, dict[str, Any]]:
    by_path = {entry["path"]: entry for entry in status_entries(root, exclude)}
    records: dict[str, dict[str, Any]] = {}
    for path in paths:
        entry = by_path.get(path)
        records[path] = file_record(
            root,
            path,
            entry["status"] if entry else None,
            entry["orig_path"] if entry else None,
        )
    return records


CYCLE_LOG_NAME = re.compile(r"cycle-(\d+)")
CYCLE_RECEIPT_NAME = re.compile(r"cycle-(\d+)\.json")


def next_cycle_number(runtime: Path) -> int:
    """Continue numbering after the highest valid cycle in logs and receipts."""
    highest = 0
    for folder, pattern in (("logs", CYCLE_LOG_NAME), ("receipts", CYCLE_RECEIPT_NAME)):
        base = runtime / folder
        if base.is_dir():
            for entry in base.iterdir():
                match = pattern.fullmatch(entry.name)
                if match:
                    highest = max(highest, int(match.group(1)))
    return highest + 1


def run_process(
    command: list[str],
    cwd: Path,
    input_text: str,
    timeout: int,
) -> tuple[int | None, str, str, str | None]:
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        try:
            stdout, stderr = process.communicate(input_text, timeout=timeout)
            return process.returncode, stdout, stderr, None
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            return 124, stdout, stderr, "timeout"
    except FileNotFoundError:
        return None, "", "executable not found", "agent_not_found"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", str(exc), "agent_error"


HUMAN_CONFIRMATION_NEGATIONS = (
    "不要",
    "なし",
    "不必要",
    "該当なし",
    "not needed",
    "not required",
    "none",
)


def requests_human_confirmation(stdout: str) -> bool:
    """True when the agent asks to stop for a human decision.

    Agents sometimes mention the marker while explicitly negating it
    (e.g. "HUMAN_CONFIRMATION: 不要"); such lines must not stop the loop.
    """
    for line in stdout.splitlines():
        if "HUMAN_CONFIRMATION" not in line and "人間の判断" not in line:
            continue
        lowered = line.lower()
        if any(negation in lowered for negation in HUMAN_CONFIRMATION_NEGATIONS):
            continue
        return True
    return False


def build_prompt(
    config: Config,
    previous: dict[str, Any] | None,
    gate: "TaskGateState | None" = None,
) -> str:
    files = "\n".join(
        f"- {name}" for name in config.design_files if (config.root / name).is_file()
    )
    previous_text = json.dumps(previous, ensure_ascii=False) if previous else "なし"

    if not config.allow_task_chaining:
        task_id = gate.task_id if gate is not None else "(unknown)"
        return f"""このプロジェクトは設計工程を完了しています。

正本となる設計資料:
{files or '- 設計資料は見つかりません'}

今回実装してよいタスクは、{config.task_file} の先頭front matterに記載された1件だけです。
task_id: {task_id}

{config.task_file} を開き、そこに書かれた背景・仕様・実装方針・非対象・完了報告の指示にすべて従ってください。設計資料やQandA、FIX_PLANの他の未着手項目を自分で選んで実装してはいけません。今回のtask_id以外のタスクへ着手せず、front matterの `task_id` の値自体も変更しないでください。

未回答のQandAやBLOCKED項目を検出しても、直ちに人間確認で停止しないでください。今回のtask_idに関係する範囲でのみ、既存仕様との整合、後方互換性、変更量、可逆性、安全性を基準に、最も妥当な技術的既定案を自動選択してください。選択した判断をQandA.mdへAUTO_DECIDEDとして記録し、必要な設計資料へ最小限反映したうえで実装してください。

人間確認が必要なのは、不可逆・破壊的変更、金銭、秘密情報、外部公開、法務、根本的な仕様変更、または安全な既定案が存在しない場合だけです。その場合は {config.task_file} の `status:` を `blocked` に書き換え、結果に理由を記録してください。作業を試みたものの完了できずエラーで終わった場合は `status:` を `failed` に書き換えてください。作業に着手済みでまだ続きがある場合は `status:` を `pending` のままにするか、作業継続中であることを示す `in_progress` にしてください。設計資料を勝手に全面変更せず、設計にない機能、不要な大規模リファクタリング、commit、push、PRを行わないでください（git commit・git pushはAgent自身が行わず、人間が確認したうえで行います）。

前回receiptの概要:
{previous_text}

今回のtask_id（{task_id}）の実装とテストが完了したら、必ず {config.task_file} の先頭front matterの `status:` の値だけを `completed` に書き換えてください（`task_id` の値は変更しないでください）。完了できなかった場合は上記のとおり `pending`・`in_progress`・`blocked`・`failed` のいずれかに書き換えてください。この `status:` の更新が、このタスクの状態をシステムへ伝える唯一の合図です。

完了時は、選択肢、自動決定、決定理由、QandAへの記録、実装内容、変更ファイル、テスト結果、未解決事項を報告してください。"""

    return f"""このプロジェクトは設計工程を完了しています。

正本となる設計資料:
{files or '- 設計資料は見つかりません'}

現在のソースコード、テスト、Git状態、前回receiptを確認し、設計済みだが未実装、またはテストが失敗している項目から、次に行う小さな実装タスクを1つだけ選んでください。選択したタスクを実装し、対応テストを実行してください。

未回答のQandAやBLOCKED項目を検出しても、直ちに人間確認で停止しないでください。関連する設計資料、コード、テスト、Git履歴を確認し、選択肢と影響を比較してください。既存仕様との整合、後方互換性、変更量、可逆性、安全性を基準に、最も妥当な技術的既定案を自動選択してください。選択した判断をQandA.mdへAUTO_DECIDEDとして記録し、必要な設計資料へ最小限反映したうえで、選択した仕様に対応する小さな実装タスクを1つ実行してください。

人間確認で停止するのは、不可逆・破壊的変更、金銭、秘密情報、外部公開、法務、根本的な仕様変更、または安全な既定案が存在しない場合だけです。その場合は結果に HUMAN_CONFIRMATION と理由を記録してください。設計資料を勝手に全面変更せず、設計にない機能、不要な大規模リファクタリング、commit、push、PRを行わないでください。

前回receiptの概要:
{previous_text}

完了時は、選択タスク、選択肢、自動決定、決定理由、QandAへの記録、実装内容、変更ファイル、テスト結果、未解決事項、次候補、プロジェクト全体が完了したかどうかを報告してください。"""


def _base_receipt(cycle: int, started: str, project_root: Path, agent_name: str) -> dict[str, Any]:
    """Receipt fields every cycle path must produce (agent-run or not).

    Early-return paths (no_pending_task, task_gate_invalid) fill these in
    with empty/None defaults instead of omitting them, so a receipt
    consumer never has to special-case which fields exist.
    """
    return {
        "cycle": cycle,
        "started_at": started,
        "finished_at": None,
        "project_root": str(project_root),
        "agent": agent_name,
        "agent_exit_code": None,
        "test_exit_codes": [],
        "changed_files": [],
        "stdout_file": None,
        "stderr_file": None,
        "decision": None,
        "before_changed_files": [],
        "tests": [],
        "agent_error": None,
        "preexisting_dirty_files": [],
        "agent_changed_files": [],
        "protected_dirty_violations": [],
        "task_gate_enabled": False,
        "task_file": None,
        "task_id": None,
        "task_status_before": None,
        "task_status_after": None,
        "task_gate_error": None,
        "repository_lock_path": None,
        "repository_lock_acquired": False,
        "repository_lock_instance_id": None,
        "repository_lock_status": None,
        "repository_lock_owner_pid": None,
        "repository_lock_started_at": None,
        "repository_lock_release_succeeded": None,
        "repository_lock_warning": None,
    }


def _lock_receipt_fields(lock: "RepositoryLock") -> dict[str, Any]:
    """Lock fields for a receipt produced while `lock` is held by this
    process. release_succeeded/release_warning are filled in later, once
    the lock is actually released at the end of run()."""
    return {
        "repository_lock_path": str(lock.path),
        "repository_lock_acquired": True,
        "repository_lock_instance_id": lock.instance_id,
        "repository_lock_status": "active",
        "repository_lock_owner_pid": os.getpid(),
        "repository_lock_started_at": lock._last_started_at,
    }


class Controller:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.runtime = config.root / config.automation_dir
        lock_mode = "chaining" if config.allow_task_chaining else "single-task"
        self.lock = RepositoryLock(
            config.root, mode=lock_mode, controller_path=str(Path(__file__).resolve())
        )
        # AutoLoop's own directories are not part of the user's work.
        self.excludes = (config.automation_dir, AUTOLOOP_CONFIG_DIR)

    def receipt_path(self, cycle: int) -> Path:
        return self.runtime / "receipts" / f"cycle-{cycle:03d}.json"

    def cycle(
        self,
        number: int,
        previous: dict[str, Any] | None,
        protected: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        run_dir = self.runtime / "logs" / f"cycle-{number:03d}"
        receipt_file = self.receipt_path(number)
        if run_dir.exists() or receipt_file.exists():
            raise ControllerError(
                f"cycle artifacts already exist, refusing to overwrite: cycle-{number:03d}"
            )

        gate_enabled = not self.config.allow_task_chaining
        started = timestamp()
        gate: TaskGateState | None = None

        if gate_enabled:
            gate = read_task_state(self.config.root, self.config.task_file)

            if not gate.valid:
                # Fail closed: missing/unreadable/malformed gate file, or a
                # missing/empty/unrecognized task_id or status. The agent is
                # never started and verification never runs.
                run_dir.mkdir(parents=True)
                receipt = _base_receipt(number, started, self.config.root, self.config.agent_name)
                receipt.update(_lock_receipt_fields(self.lock))
                receipt.update(
                    finished_at=timestamp(),
                    changed_files=changed_files(self.config.root, self.excludes),
                    decision="task_gate_invalid",
                    preexisting_dirty_files=sorted(protected),
                    task_gate_enabled=True,
                    task_file=self.config.task_file,
                    task_id=gate.task_id,
                    task_status_before=gate.status,
                    task_gate_error=gate.error,
                )
                receipt_file.parent.mkdir(parents=True, exist_ok=True)
                receipt_file.write_text(
                    json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return receipt

            if gate.status not in ACTIONABLE_TASK_STATUSES:
                # Nothing approved to work on right now (completed/blocked/
                # failed): stop without spending an agent call. max_cycles is
                # a retry budget for the one pending task_id, not a license
                # to look for other work.
                run_dir.mkdir(parents=True)
                receipt = _base_receipt(number, started, self.config.root, self.config.agent_name)
                receipt.update(_lock_receipt_fields(self.lock))
                receipt.update(
                    finished_at=timestamp(),
                    changed_files=changed_files(self.config.root, self.excludes),
                    decision="no_pending_task",
                    preexisting_dirty_files=sorted(protected),
                    task_gate_enabled=True,
                    task_file=self.config.task_file,
                    task_id=gate.task_id,
                    task_status_before=gate.status,
                    task_status_after=gate.status,
                )
                receipt_file.parent.mkdir(parents=True, exist_ok=True)
                receipt_file.write_text(
                    json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return receipt

        run_dir.mkdir(parents=True)
        before = changed_files(self.config.root, self.excludes)
        prompt = build_prompt(self.config, previous, gate)

        if self.config.prompt_delivery == "stdin":
            agent_command, agent_input = self.config.agent_command, prompt
        else:
            agent_command, agent_input = [*self.config.agent_command, prompt], ""

        exit_code, stdout, stderr, error = run_process(
            agent_command,
            self.config.root,
            agent_input,
            self.config.agent_timeout_seconds,
        )
        stdout_file = run_dir / "stdout.txt"
        stderr_file = run_dir / "stderr.txt"
        stdout_file.write_text(stdout, encoding="utf-8")
        stderr_file.write_text(stderr, encoding="utf-8")

        tests: list[dict[str, Any]] = []
        if error is None and exit_code is not None:
            for index, command in enumerate(self.config.verification_commands, 1):
                code, out, err, test_error = run_process(
                    command,
                    self.config.root,
                    "",
                    self.config.agent_timeout_seconds,
                )
                (run_dir / f"test-{index}-stdout.txt").write_text(out, encoding="utf-8")
                (run_dir / f"test-{index}-stderr.txt").write_text(err, encoding="utf-8")
                tests.append(
                    {"command": command, "exit_code": code, "error": test_error}
                )

        after = changed_files(self.config.root, self.excludes)
        current = snapshot(self.config.root, sorted(protected), self.excludes)
        violations = []
        for path, record in protected.items():
            fields = [key for key in record if current[path].get(key) != record.get(key)]
            if fields:
                violations.append({"path": path, "changes": fields})
        no_change = before == after
        test_failed = any(item["exit_code"] != 0 for item in tests)
        human = requests_human_confirmation(stdout)
        complete = "PROJECT_COMPLETE" in stdout or "プロジェクト全体が完了" in stdout

        gate_after: TaskGateState | None = None
        task_id_changed = False
        task_completed = False
        task_needs_human = False
        task_gate_broken_after = False
        if gate_enabled and gate is not None:
            gate_after = read_task_state(self.config.root, self.config.task_file)
            if not gate_after.valid:
                # The agent left the gate file broken (deleted, malformed,
                # unrecognized status, ...). Fail closed the same way an
                # invalid gate file at cycle start would.
                task_gate_broken_after = True
            else:
                task_id_changed = gate_after.task_id != gate.task_id
                if not task_id_changed:
                    if gate_after.status == "completed":
                        task_completed = True
                    elif gate_after.status in ("blocked", "failed"):
                        # Agent recorded it cannot/did not finish. Stop and
                        # wait for a human; never auto-advance to another
                        # task_id on its own.
                        task_needs_human = True
                    # "pending"/"in_progress": falls through to the normal
                    # continue/no_change handling below, same as before the
                    # gate existed - max_cycles keeps retrying this task_id.

        if error:
            decision = error
        elif violations:
            decision = "protected_dirty_changed"
        elif exit_code != 0 and self.config.stop_on_agent_failure:
            decision = "agent_failed"
        elif test_failed and self.config.stop_on_test_failure:
            decision = "test_failed"
        elif task_id_changed:
            # allow_task_chaining is off: the gate file's task_id is the
            # only approved unit of work. The agent changing it on its own
            # is treated the same as any other decision only a human should
            # make, not a routine "continue".
            decision = "human_confirmation"
        elif task_gate_broken_after:
            decision = "task_gate_invalid"
        elif task_completed:
            decision = "completed"
        elif task_needs_human:
            decision = "human_confirmation"
        elif human:
            decision = "human_confirmation"
        elif complete:
            decision = "completed"
        elif no_change and previous and previous.get("changed_files", []) == after:
            decision = "no_change"
        else:
            decision = "continue"

        receipt = _base_receipt(number, started, self.config.root, self.config.agent_name)
        receipt.update(_lock_receipt_fields(self.lock))
        receipt.update(
            finished_at=timestamp(),
            agent_exit_code=exit_code,
            test_exit_codes=[item["exit_code"] for item in tests],
            changed_files=after,
            stdout_file=str(stdout_file),
            stderr_file=str(stderr_file),
            decision=decision,
            before_changed_files=before,
            tests=tests,
            agent_error=error,
            preexisting_dirty_files=sorted(protected),
            agent_changed_files=[path for path in after if path not in protected],
            protected_dirty_violations=violations,
            task_gate_enabled=gate_enabled,
        )
        if gate_enabled and gate is not None:
            receipt.update(
                task_file=self.config.task_file,
                task_id=gate.task_id,
                task_status_before=gate.status,
                task_status_after=(gate_after.status if gate_after is not None and gate_after.valid else None),
                task_gate_error=(gate_after.error if gate_after is not None and not gate_after.valid else None),
            )
        receipt_file.parent.mkdir(parents=True, exist_ok=True)
        receipt_file.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return receipt

    def dirty_gate(self) -> tuple[list[str], list[str]]:
        """Return dirty paths and the subset not covered by allowed_dirty_paths."""
        dirty = changed_files(self.config.root, self.excludes)
        allowed = self.config.allowed_dirty_paths
        disallowed = [
            path
            for path in dirty
            if not any(path == item or path.startswith(f"{item}/") for item in allowed)
        ]
        return dirty, disallowed

    def run(self, once: bool = False) -> list[dict[str, Any]]:
        try:
            self.lock.acquire()
        except LockAcquisitionError as exc:
            decision = {
                "active": "repository_locked",
                "foreign_host": "repository_locked",
                "stale": "stale_lock",
                "invalid": "invalid_lock_file",
                "unknown": "repository_locked",
            }.get(exc.status.state, "lock_acquisition_failed")
            record = exc.status.record
            print(
                f"ERROR: repository lock not acquired ({exc.status.state}), agent not started",
                file=sys.stderr,
            )
            return [
                {
                    "decision": decision,
                    "finished_at": timestamp(),
                    "repository_lock_path": str(self.lock.path),
                    "repository_lock_acquired": False,
                    "repository_lock_instance_id": None,
                    "repository_lock_status": exc.status.state,
                    "repository_lock_owner_pid": record.pid if record else None,
                    "repository_lock_started_at": record.started_at if record else None,
                    "repository_lock_release_succeeded": None,
                    "repository_lock_warning": exc.status.error,
                }
            ]

        print(
            "AutoLoop acquired the repository lock. Do not edit this worktree from "
            "another AI coding session or IDE until AutoLoop exits.",
            file=sys.stderr,
        )
        receipts: list[dict[str, Any]] = []
        try:
            dirty, disallowed = self.dirty_gate()
            if dirty and (not self.config.allow_dirty_worktree or disallowed):
                blocked = dirty if not self.config.allow_dirty_worktree else disallowed
                print(
                    f"ERROR: dirty worktree, agent not started: {', '.join(blocked)}",
                    file=sys.stderr,
                )
                receipts.append(
                    {
                        "decision": "dirty_worktree",
                        "dirty_paths": dirty,
                        "disallowed_paths": disallowed,
                        "finished_at": timestamp(),
                        **_lock_receipt_fields(self.lock),
                    }
                )
                return receipts
            protected = snapshot(self.config.root, dirty, self.excludes)
            start = next_cycle_number(self.runtime)
            for offset in range(1 if once else self.config.max_cycles):
                previous = receipts[-1] if receipts else None
                receipt = self.cycle(start + offset, previous, protected)
                receipts.append(receipt)
                if receipt["decision"] != "continue":
                    break
            return receipts
        except KeyboardInterrupt:
            receipts.append(
                {
                    "decision": "interrupted",
                    "finished_at": timestamp(),
                    **_lock_receipt_fields(self.lock),
                }
            )
            return receipts
        finally:
            succeeded, warning = self.lock.release()
            if warning:
                print(f"warning: {warning}", file=sys.stderr)
            for receipt in receipts:
                receipt["repository_lock_release_succeeded"] = succeeded
                receipt["repository_lock_warning"] = warning


SUCCESS_DECISIONS = frozenset({"completed", "continue", "no_change", "no_pending_task"})
HUMAN_CONFIRMATION_EXIT_CODE = 2


def exit_code_for(receipts: list[dict[str, Any]]) -> int:
    """Map the final decision to the controller process exit code."""
    decision = receipts[-1].get("decision") if receipts else None
    if decision in SUCCESS_DECISIONS:
        return 0
    if decision == "human_confirmation":
        return HUMAN_CONFIRMATION_EXIT_CODE
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run an AI coding agent repeatedly against a target Git repository."
    )
    parser.add_argument(
        "--project",
        type=Path,
        help="Target project directory. Defaults to the current directory.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Config path. Relative paths are resolved from the target project.",
    )
    parser.add_argument("--once", action="store_true", help="Run exactly one cycle.")
    parser.add_argument(
        "--lock-status",
        action="store_true",
        help="Report the repository lock state and exit. Never acquires the "
        "lock or starts the agent.",
    )
    parser.add_argument(
        "--unlock-stale",
        action="store_true",
        help="Remove the repository lock file only if it is unambiguously "
        "stale (owning process no longer running). Never touches an "
        "active, foreign_host, invalid, or unknown lock. Never starts "
        "the agent.",
    )
    args = parser.parse_args(argv)

    try:
        root = resolve_project_root(args.project)

        if args.lock_status:
            status = RepositoryLock(root).status()
            print(json.dumps(safe_lock_status(status), ensure_ascii=False, indent=2))
            return 0

        if args.unlock_stale:
            status = RepositoryLock(root).unlock_stale()
            print(json.dumps(safe_lock_status(status), ensure_ascii=False, indent=2))
            return 0 if status.state in ("stale", "none") else 1

        config_path = resolve_config_path(args.config, root)
        config = Config.load(config_path, root)
        result = Controller(config).run(args.once)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_code_for(result)
    except ControllerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
