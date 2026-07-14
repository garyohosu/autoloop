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
import subprocess
import sys
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

    @classmethod
    def load(cls, path: Path, root: Path | None = None) -> "Config":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
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
        )


class Lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "started_at": timestamp()}, stream)
            self.held = True
        except FileExistsError as exc:
            raise ControllerError("another AutoLoop is already running") from exc

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError as exc:
                print(f"warning: lock release failed: {exc}", file=sys.stderr)
            self.held = False


def _clean_status_path(value: str) -> str:
    return value.strip().strip('"').replace("\\", "/")


def status_entries(root: Path, exclude: str | None = None) -> list[dict[str, str | None]]:
    """Parse `git status --porcelain -uall`, optionally excluding one directory."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ControllerError(result.stderr.strip() or "git status failed")
    prefix = f"{exclude}/" if exclude else None
    entries: list[dict[str, str | None]] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        status, value = line[:2], line[3:]
        orig = None
        if " -> " in value:
            orig, value = value.split(" -> ", 1)
        path = _clean_status_path(value)
        if prefix and (path == exclude or path.startswith(prefix)):
            continue
        entries.append(
            {
                "status": status,
                "path": path,
                "orig_path": _clean_status_path(orig) if orig else None,
            }
        )
    return entries


def changed_files(root: Path, exclude: str | None = None) -> list[str]:
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


def snapshot(root: Path, paths: list[str], exclude: str | None = None) -> dict[str, dict[str, Any]]:
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


def build_prompt(config: Config, previous: dict[str, Any] | None) -> str:
    files = "\n".join(
        f"- {name}" for name in config.design_files if (config.root / name).is_file()
    )
    previous_text = json.dumps(previous, ensure_ascii=False) if previous else "なし"
    return f"""このプロジェクトは設計工程を完了しています。

正本となる設計資料:
{files or '- 設計資料は見つかりません'}

現在のソースコード、テスト、Git状態、前回receiptを確認し、設計済みだが未実装、またはテストが失敗している項目から、次に行う小さな実装タスクを1つだけ選んでください。選択したタスクを実装し、対応テストを実行してください。

未回答のQandAやBLOCKED項目を検出しても、直ちに人間確認で停止しないでください。関連する設計資料、コード、テスト、Git履歴を確認し、選択肢と影響を比較してください。既存仕様との整合、後方互換性、変更量、可逆性、安全性を基準に、最も妥当な技術的既定案を自動選択してください。選択した判断をQandA.mdへAUTO_DECIDEDとして記録し、必要な設計資料へ最小限反映したうえで、選択した仕様に対応する小さな実装タスクを1つ実行してください。

人間確認で停止するのは、不可逆・破壊的変更、金銭、秘密情報、外部公開、法務、根本的な仕様変更、または安全な既定案が存在しない場合だけです。その場合は結果に HUMAN_CONFIRMATION と理由を記録してください。設計資料を勝手に全面変更せず、設計にない機能、不要な大規模リファクタリング、commit、push、PRを行わないでください。

前回receiptの概要:
{previous_text}

完了時は、選択タスク、選択肢、自動決定、決定理由、QandAへの記録、実装内容、変更ファイル、テスト結果、未解決事項、次候補、プロジェクト全体の完了 여부を報告してください。"""


class Controller:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.runtime = config.root / config.automation_dir
        self.lock = Lock(self.runtime / "autoloop.lock")

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
        started = timestamp()
        run_dir.mkdir(parents=True)
        before = changed_files(self.config.root, self.config.automation_dir)
        prompt = build_prompt(self.config, previous)

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

        after = changed_files(self.config.root, self.config.automation_dir)
        current = snapshot(self.config.root, sorted(protected), self.config.automation_dir)
        violations = []
        for path, record in protected.items():
            fields = [key for key in record if current[path].get(key) != record.get(key)]
            if fields:
                violations.append({"path": path, "changes": fields})
        no_change = before == after
        test_failed = any(item["exit_code"] != 0 for item in tests)
        human = requests_human_confirmation(stdout)
        complete = "PROJECT_COMPLETE" in stdout or "プロジェクト全体が完了" in stdout

        if error:
            decision = error
        elif violations:
            decision = "protected_dirty_changed"
        elif exit_code != 0 and self.config.stop_on_agent_failure:
            decision = "agent_failed"
        elif test_failed and self.config.stop_on_test_failure:
            decision = "test_failed"
        elif human:
            decision = "human_confirmation"
        elif complete:
            decision = "completed"
        elif no_change and previous and previous.get("changed_files", []) == after:
            decision = "no_change"
        else:
            decision = "continue"

        receipt = {
            "cycle": number,
            "started_at": started,
            "finished_at": timestamp(),
            "project_root": str(self.config.root),
            "agent": self.config.agent_name,
            "agent_exit_code": exit_code,
            "test_exit_codes": [item["exit_code"] for item in tests],
            "changed_files": after,
            "stdout_file": str(stdout_file),
            "stderr_file": str(stderr_file),
            "decision": decision,
            "before_changed_files": before,
            "tests": tests,
            "agent_error": error,
            "preexisting_dirty_files": sorted(protected),
            "agent_changed_files": [path for path in after if path not in protected],
            "protected_dirty_violations": violations,
        }
        receipt_file.parent.mkdir(parents=True, exist_ok=True)
        receipt_file.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return receipt

    def dirty_gate(self) -> tuple[list[str], list[str]]:
        """Return dirty paths and the subset not covered by allowed_dirty_paths."""
        dirty = changed_files(self.config.root, self.config.automation_dir)
        allowed = self.config.allowed_dirty_paths
        disallowed = [
            path
            for path in dirty
            if not any(path == item or path.startswith(f"{item}/") for item in allowed)
        ]
        return dirty, disallowed

    def run(self, once: bool = False) -> list[dict[str, Any]]:
        self.lock.acquire()
        receipts: list[dict[str, Any]] = []
        try:
            dirty, disallowed = self.dirty_gate()
            if dirty and (not self.config.allow_dirty_worktree or disallowed):
                blocked = dirty if not self.config.allow_dirty_worktree else disallowed
                print(
                    f"ERROR: dirty worktree, agent not started: {', '.join(blocked)}",
                    file=sys.stderr,
                )
                return [
                    {
                        "decision": "dirty_worktree",
                        "dirty_paths": dirty,
                        "disallowed_paths": disallowed,
                        "finished_at": timestamp(),
                    }
                ]
            protected = snapshot(self.config.root, dirty, self.config.automation_dir)
            start = next_cycle_number(self.runtime)
            for offset in range(1 if once else self.config.max_cycles):
                previous = receipts[-1] if receipts else None
                receipt = self.cycle(start + offset, previous, protected)
                receipts.append(receipt)
                if receipt["decision"] != "continue":
                    break
            return receipts
        except KeyboardInterrupt:
            return receipts + [{"decision": "interrupted", "finished_at": timestamp()}]
        finally:
            self.lock.release()


SUCCESS_DECISIONS = frozenset({"completed", "continue", "no_change"})
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
    args = parser.parse_args(argv)

    try:
        root = resolve_project_root(args.project)
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
