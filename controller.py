"""Lightweight controller for iterative AI-assisted development.

AutoLoop runs a configured coding agent inside a target Git repository, executes
fixed verification commands, and stores receipts and logs in that target
repository. It never commits or pushes changes.
"""
from __future__ import annotations

import argparse
import json
import os
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


def changed_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ControllerError(result.stderr.strip() or "git status failed")
    files: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) >= 4:
            value = line[3:].strip()
            if " -> " in value:
                value = value.split(" -> ")[-1]
            files.append(value.replace("\\", "/"))
    return sorted(set(files))


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

    def cycle(self, number: int, previous: dict[str, Any] | None) -> dict[str, Any]:
        started = timestamp()
        run_dir = self.runtime / "logs" / f"cycle-{number:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        before = changed_files(self.config.root)
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

        after = changed_files(self.config.root)
        no_change = before == after
        test_failed = any(item["exit_code"] != 0 for item in tests)
        human = "HUMAN_CONFIRMATION" in stdout or "人間の判断" in stdout
        complete = "PROJECT_COMPLETE" in stdout or "プロジェクト全体が完了" in stdout

        if error:
            decision = error
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
        }
        path = self.receipt_path(number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        return receipt

    def run(self, once: bool = False) -> list[dict[str, Any]]:
        self.lock.acquire()
        receipts: list[dict[str, Any]] = []
        try:
            for cycle in range(1, 2 if once else self.config.max_cycles + 1):
                previous = receipts[-1] if receipts else None
                receipt = self.cycle(cycle, previous)
                receipts.append(receipt)
                if receipt["decision"] != "continue":
                    break
            return receipts
        except KeyboardInterrupt:
            return receipts + [{"decision": "interrupted", "finished_at": timestamp()}]
        finally:
            self.lock.release()


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
        return 0
    except ControllerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
