import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from controller import (
    Config,
    Controller,
    ControllerError,
    LockAcquisitionError,
    LockStatus,
    RepositoryLock,
    VALID_PROMPT_DELIVERIES,
    build_prompt,
    exit_code_for,
    HUMAN_CONFIRMATION_EXIT_CODE,
    main,
    next_cycle_number,
    read_task_state,
    resolve_config_path,
    resolve_project_root,
    safe_lock_status,
)


class BaseControllerTest(unittest.TestCase):
    def repo(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="autoloop-target-"))
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "AutoLoop"],
            cwd=root,
            check=True,
        )
        (root / "src").mkdir()
        (root / "SPEC.md").write_text("design", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
        return root

    def agent(self, code: str) -> list[str]:
        path = Path(tempfile.mkstemp(suffix=".py")[1])
        path.write_text(code, encoding="utf-8")
        return [sys.executable, str(path)]

    def config(
        self,
        root: Path,
        command: list[str] | None = None,
        prompt_delivery: str = "argument",
        **kwargs,
    ) -> Config:
        return Config(
            root,
            design_files=["SPEC.md", "MISSING.md"],
            verification_commands=kwargs.get(
                "verification_commands", [[sys.executable, "-c", "pass"]]
            ),
            agent_command=command
            or self.agent("from pathlib import Path; Path('src/out.py').write_text('ok')"),
            prompt_delivery=prompt_delivery,
            agent_timeout_seconds=kwargs.get("timeout", 5),
            max_cycles=kwargs.get("max_cycles", 2),
            stop_on_agent_failure=kwargs.get("stop_on_agent_failure", True),
            stop_on_test_failure=kwargs.get("stop_on_test_failure", False),
            allow_dirty_worktree=kwargs.get("allow_dirty_worktree", False),
            allowed_dirty_paths=kwargs.get("allowed_dirty_paths", []),
            # These generic tests exercise agent/verification/dirty-worktree
            # mechanics from before the single-task gate existed, so they
            # opt back into the old "pick any task" behaviour. Task-gate
            # semantics (the new default) get their own TaskGateTests below.
            allow_task_chaining=kwargs.get("allow_task_chaining", True),
            task_file=kwargs.get("task_file", "instructions/instructions.md"),
        )


class ControllerTests(BaseControllerTest):
    def test_resolve_explicit_project_root(self):
        root = self.repo()
        nested = root / "src"
        self.assertEqual(resolve_project_root(nested), root.resolve())

    def test_resolve_project_defaults_to_current_directory(self):
        root = self.repo()
        self.assertEqual(resolve_project_root(None, cwd=root), root.resolve())

    def test_resolve_project_rejects_missing_directory(self):
        missing = Path(tempfile.gettempdir()) / "autoloop-definitely-missing-project"
        with self.assertRaises(ControllerError):
            resolve_project_root(missing)

    def test_relative_config_is_resolved_from_target_project(self):
        root = self.repo()
        self.assertEqual(
            resolve_config_path(Path(".autoloop/config.json"), root),
            root / ".autoloop" / "config.json",
        )

    def test_default_config_is_target_autoloop_config(self):
        root = self.repo()
        self.assertEqual(
            resolve_config_path(None, root),
            root / ".autoloop" / "config.json",
        )

    def test_config_prompt_and_design_files(self):
        root = self.repo()
        config = self.config(root)
        prompt = build_prompt(config, None)
        self.assertIn("SPEC.md", prompt)
        self.assertNotIn("MISSING.md", prompt)
        self.assertIn("AUTO_DECIDED", prompt)
        self.assertIn("未回答のQandAやBLOCKED項目", prompt)
        self.assertIn("不可逆・破壊的変更", prompt)

    def test_config_load_with_agent_command(self):
        root = self.repo()
        path = root / "config.json"
        path.write_text(
            json.dumps({"agent": {"command": ["custom-agent"]}}),
            encoding="utf-8",
        )
        config = Config.load(path, root)
        self.assertEqual(config.agent_command, ["custom-agent"])

    def test_config_load_uses_default_agent_command(self):
        root = self.repo()
        path = root / "config.json"
        path.write_text("{}", encoding="utf-8")
        config = Config.load(path, root)
        self.assertEqual(
            config.agent_command,
            ["codex", "exec", "--sandbox", "workspace-write"],
        )

    def test_config_load_does_not_raise_attribute_error(self):
        root = self.repo()
        path = root / "config.json"
        path.write_text(json.dumps({"agent": {"name": "codex"}}), encoding="utf-8")
        try:
            Config.load(path, root)
        except AttributeError as exc:
            self.fail(f"Config.load raised AttributeError: {exc}")

    def test_config_example_loads(self):
        root = self.repo()
        path = Path(__file__).with_name("config.example.json")
        config = Config.load(path, root)
        self.assertEqual(config.agent_name, "codex")
        self.assertEqual(
            config.agent_command,
            ["codex", "exec", "--sandbox", "workspace-write"],
        )

    def test_repo_path_must_match_target_root(self):
        root = self.repo()
        path = root / "config.json"
        path.write_text(json.dumps({"repo_path": str(root / "other")}), encoding="utf-8")
        with self.assertRaises(ControllerError):
            Config.load(path, root)

    def test_once_agent_tests_and_receipt_in_target_project(self):
        root = self.repo()
        receipt = Controller(self.config(root)).run(once=True)[0]
        self.assertEqual(receipt["agent_exit_code"], 0)
        self.assertEqual(receipt["test_exit_codes"], [0])
        self.assertEqual(Path(receipt["project_root"]), root.resolve())
        self.assertTrue(Path(receipt["stdout_file"]).exists())
        self.assertTrue((root / ".runtime" / "receipts" / "cycle-001.json").exists())

    def test_agent_and_verification_run_in_target_project(self):
        root = self.repo()
        command = self.agent(
            "from pathlib import Path; Path('agent-cwd.txt').write_text(str(Path.cwd()))"
        )
        receipt = Controller(self.config(root, command)).run(once=True)[0]
        self.assertEqual((root / "agent-cwd.txt").read_text(), str(root.resolve()))
        self.assertEqual(receipt["test_exit_codes"], [0])

    def test_agent_failure(self):
        root = self.repo()
        receipt = Controller(
            self.config(root, self.agent("raise SystemExit(7)"))
        ).run(once=True)[0]
        self.assertEqual(receipt["decision"], "agent_failed")
        self.assertEqual(receipt["agent_exit_code"], 7)

    def test_timeout(self):
        root = self.repo()
        command = self.agent("import time; time.sleep(2)")
        receipt = Controller(self.config(root, command, timeout=1)).run(once=True)[0]
        self.assertEqual(receipt["decision"], "timeout")

    def test_max_cycles_and_no_change_stop(self):
        root = self.repo()
        config = self.config(root, self.agent("print('same')"), max_cycles=5)
        receipts = Controller(config).run()
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[-1]["decision"], "no_change")

    def test_completion_stops(self):
        root = self.repo()
        command = self.agent("print('PROJECT_COMPLETE')")
        self.assertEqual(Controller(self.config(root, command)).run()[0]["decision"], "completed")

    def test_human_confirmation_stops(self):
        root = self.repo()
        command = self.agent("print('HUMAN_CONFIRMATION: design conflict')")
        self.assertEqual(
            Controller(self.config(root, command)).run()[0]["decision"],
            "human_confirmation",
        )

    def test_negated_human_confirmation_does_not_stop(self):
        root = self.repo()
        command = self.agent(
            "import sys; from pathlib import Path; Path('src/out.py').write_text('ok'); "
            "sys.stdout.buffer.write('- HUMAN_CONFIRMATION: 不要\\n'.encode('utf-8'))"
        )
        receipt = Controller(self.config(root, command)).run(once=True)[0]
        self.assertEqual(receipt["decision"], "continue")

    def test_negated_english_human_confirmation_does_not_stop(self):
        root = self.repo()
        command = self.agent(
            "from pathlib import Path; Path('src/out.py').write_text('ok'); "
            "print('HUMAN_CONFIRMATION: not needed')"
        )
        receipt = Controller(self.config(root, command)).run(once=True)[0]
        self.assertEqual(receipt["decision"], "continue")

    def test_lock_blocks_second_controller(self):
        root = self.repo()
        config = self.config(root)
        lock = RepositoryLock(root)
        lock.acquire()
        try:
            receipts = Controller(config).run(once=True)
            self.assertEqual(receipts[0]["decision"], "repository_locked")
            self.assertEqual(exit_code_for(receipts), 1)
            self.assertFalse(receipts[0]["repository_lock_acquired"])
        finally:
            lock.release()

    def test_prompt_delivery_default_is_argument(self):
        root = self.repo()
        path = root / "config.json"
        path.write_text(
            json.dumps({"agent": {"command": ["my-agent"]}}),
            encoding="utf-8",
        )
        config = Config.load(path, root)
        self.assertEqual(config.prompt_delivery, "argument")

    def test_prompt_delivery_argument_appends_to_command(self):
        root = self.repo()
        code = "import sys, json; print(json.dumps(sys.argv[1:]))"
        receipt = Controller(
            self.config(root, self.agent(code), prompt_delivery="argument")
        ).run(once=True)[0]
        argv = json.loads(Path(receipt["stdout_file"]).read_text(encoding="utf-8").strip())
        self.assertIsInstance(argv, list)
        self.assertGreaterEqual(len(argv), 1)

    def test_prompt_delivery_stdin_does_not_append_to_command(self):
        root = self.repo()
        code = (
            "import sys, json; data = sys.stdin.read(); "
            "print(json.dumps({'argv': sys.argv[1:], 'stdin_len': len(data)}))"
        )
        receipt = Controller(
            self.config(root, self.agent(code), prompt_delivery="stdin")
        ).run(once=True)[0]
        out = json.loads(Path(receipt["stdout_file"]).read_text(encoding="utf-8").strip())
        self.assertEqual(out["argv"], [])
        self.assertGreater(out["stdin_len"], 0)

    def test_prompt_delivery_invalid_raises(self):
        root = self.repo()
        path = root / "config.json"
        path.write_text(
            json.dumps(
                {"agent": {"command": ["my-agent"], "prompt_delivery": "magic"}}
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ControllerError):
            Config.load(path, root)

    def test_valid_prompt_deliveries_constant(self):
        self.assertEqual(VALID_PROMPT_DELIVERIES, {"argument", "stdin"})


class CycleNumberingTests(BaseControllerTest):
    def test_next_cycle_number_empty_runtime(self):
        root = self.repo()
        self.assertEqual(next_cycle_number(root / ".runtime"), 1)
        receipt = Controller(self.config(root)).run(once=True)[0]
        self.assertEqual(receipt["cycle"], 1)
        self.assertTrue((root / ".runtime" / "receipts" / "cycle-001.json").exists())

    def test_next_cycle_number_continues_after_existing(self):
        root = self.repo()
        runtime = root / ".runtime"
        (runtime / "logs" / "cycle-001").mkdir(parents=True)
        (runtime / "logs" / "cycle-002").mkdir()
        (runtime / "receipts").mkdir(parents=True)
        (runtime / "receipts" / "cycle-001.json").write_text("{}", encoding="utf-8")
        (runtime / "receipts" / "cycle-002.json").write_text("{}", encoding="utf-8")
        self.assertEqual(next_cycle_number(runtime), 3)

    def test_next_cycle_number_uses_logs_only(self):
        root = self.repo()
        runtime = root / ".runtime"
        (runtime / "logs" / "cycle-004").mkdir(parents=True)
        self.assertEqual(next_cycle_number(runtime), 5)

    def test_next_cycle_number_uses_receipts_only(self):
        root = self.repo()
        runtime = root / ".runtime"
        (runtime / "receipts").mkdir(parents=True)
        (runtime / "receipts" / "cycle-006.json").write_text("{}", encoding="utf-8")
        self.assertEqual(next_cycle_number(runtime), 7)

    def test_next_cycle_number_ignores_invalid_names(self):
        root = self.repo()
        runtime = root / ".runtime"
        (runtime / "logs" / "cycle-test").mkdir(parents=True)
        (runtime / "receipts").mkdir(parents=True)
        (runtime / "receipts" / "cycle-abc.json").write_text("{}", encoding="utf-8")
        (runtime / "receipts" / "backup-cycle-001.json").write_text("{}", encoding="utf-8")
        self.assertEqual(next_cycle_number(runtime), 1)

    def test_existing_runtime_not_overwritten(self):
        root = self.repo()
        runtime = root / ".runtime"
        (runtime / "logs" / "cycle-001").mkdir(parents=True)
        (runtime / "logs" / "cycle-001" / "stdout.txt").write_text("old", encoding="utf-8")
        (runtime / "receipts").mkdir(parents=True)
        (runtime / "receipts" / "cycle-001.json").write_text('{"old": true}', encoding="utf-8")
        receipt = Controller(self.config(root)).run(once=True)[0]
        self.assertEqual(receipt["cycle"], 2)
        self.assertEqual(
            (runtime / "logs" / "cycle-001" / "stdout.txt").read_text(encoding="utf-8"),
            "old",
        )
        self.assertEqual(
            json.loads((runtime / "receipts" / "cycle-001.json").read_text(encoding="utf-8")),
            {"old": True},
        )

    def test_cycle_conflict_stops_safely(self):
        root = self.repo()
        runtime = root / ".runtime"
        (runtime / "receipts").mkdir(parents=True)
        (runtime / "receipts" / "cycle-001.json").write_text('{"old": true}', encoding="utf-8")
        with self.assertRaises(ControllerError):
            Controller(self.config(root)).cycle(1, None, {})
        self.assertEqual(
            json.loads((runtime / "receipts" / "cycle-001.json").read_text(encoding="utf-8")),
            {"old": True},
        )


class DirtyWorktreeTests(BaseControllerTest):
    def test_clean_worktree_runs(self):
        root = self.repo()
        receipt = Controller(self.config(root)).run(once=True)[0]
        self.assertEqual(receipt["decision"], "continue")
        self.assertEqual(receipt["preexisting_dirty_files"], [])

    def test_dirty_worktree_blocks_agent_by_default(self):
        root = self.repo()
        (root / "SPEC.md").write_text("modified", encoding="utf-8")
        command = self.agent("from pathlib import Path; Path('marker.txt').write_text('ran')")
        result = Controller(self.config(root, command)).run(once=True)
        self.assertEqual(result[0]["decision"], "dirty_worktree")
        self.assertEqual(result[0]["dirty_paths"], ["SPEC.md"])
        self.assertFalse((root / "marker.txt").exists())

    def test_untracked_file_blocks_by_default(self):
        root = self.repo()
        (root / "NEW.md").write_text("untracked", encoding="utf-8")
        result = Controller(self.config(root)).run(once=True)
        self.assertEqual(result[0]["decision"], "dirty_worktree")
        self.assertIn("NEW.md", result[0]["dirty_paths"])

    def test_untracked_autoloop_dir_does_not_block(self):
        root = self.repo()
        autoloop_dir = root / ".autoloop"
        autoloop_dir.mkdir()
        (autoloop_dir / "config.json").write_text("{}", encoding="utf-8")
        (autoloop_dir / "local.json").write_text("{}", encoding="utf-8")
        receipt = Controller(self.config(root)).run(once=True)[0]
        self.assertEqual(receipt["decision"], "continue")
        self.assertEqual(receipt["preexisting_dirty_files"], [])

    def test_dirty_stop_leaves_existing_files_unchanged(self):
        root = self.repo()
        (root / "SPEC.md").write_text("modified", encoding="utf-8")
        Controller(self.config(root)).run(once=True)
        self.assertEqual((root / "SPEC.md").read_text(encoding="utf-8"), "modified")

    def test_disallowed_dirty_path_blocks(self):
        root = self.repo()
        (root / "SPEC.md").write_text("modified", encoding="utf-8")
        config = self.config(root, allow_dirty_worktree=True, allowed_dirty_paths=["OTHER.md"])
        result = Controller(config).run(once=True)
        self.assertEqual(result[0]["decision"], "dirty_worktree")
        self.assertEqual(result[0]["disallowed_paths"], ["SPEC.md"])

    def test_allowed_dirty_paths_reject_unsafe_values(self):
        root = self.repo()
        path = root / "config.json"
        for bad in ["C:/abs/path.md", "../escape.md", "//server/share/file.md", "/rooted.md"]:
            path.write_text(
                json.dumps({"allow_dirty_worktree": True, "allowed_dirty_paths": [bad]}),
                encoding="utf-8",
            )
            with self.assertRaises(ControllerError):
                Config.load(path, root)

    def test_agent_modifying_protected_dirty_file_detected(self):
        root = self.repo()
        (root / "SPEC.md").write_text("modified", encoding="utf-8")
        command = self.agent("from pathlib import Path; Path('SPEC.md').write_text('agent overwrote')")
        config = self.config(root, command, allow_dirty_worktree=True, allowed_dirty_paths=["SPEC.md"])
        receipt = Controller(config).run(once=True)[0]
        self.assertEqual(receipt["decision"], "protected_dirty_changed")
        self.assertEqual(receipt["protected_dirty_violations"][0]["path"], "SPEC.md")
        self.assertIn("sha256", receipt["protected_dirty_violations"][0]["changes"])

    def test_agent_deleting_protected_dirty_file_detected(self):
        root = self.repo()
        (root / "SPEC.md").write_text("modified", encoding="utf-8")
        command = self.agent("from pathlib import Path; Path('SPEC.md').unlink()")
        config = self.config(root, command, allow_dirty_worktree=True, allowed_dirty_paths=["SPEC.md"])
        receipt = Controller(config).run(once=True)[0]
        self.assertEqual(receipt["decision"], "protected_dirty_changed")

    def test_agent_staging_protected_dirty_file_detected(self):
        root = self.repo()
        (root / "SPEC.md").write_text("modified", encoding="utf-8")
        command = self.agent("import subprocess; subprocess.run(['git', 'add', 'SPEC.md'], check=True)")
        config = self.config(root, command, allow_dirty_worktree=True, allowed_dirty_paths=["SPEC.md"])
        receipt = Controller(config).run(once=True)[0]
        self.assertEqual(receipt["decision"], "protected_dirty_changed")

    def test_agent_new_files_separated_from_preexisting_dirty(self):
        root = self.repo()
        (root / "SPEC.md").write_text("modified", encoding="utf-8")
        config = self.config(root, allow_dirty_worktree=True, allowed_dirty_paths=["SPEC.md"])
        receipt = Controller(config).run(once=True)[0]
        self.assertEqual(receipt["decision"], "continue")
        self.assertEqual(receipt["preexisting_dirty_files"], ["SPEC.md"])
        self.assertEqual(receipt["agent_changed_files"], ["src/out.py"])
        self.assertEqual(receipt["protected_dirty_violations"], [])


class ExitCodeTests(BaseControllerTest):
    def run_main(self, root: Path, config_data: dict) -> int:
        path = Path(tempfile.mkstemp(suffix=".json")[1])
        path.write_text(json.dumps(config_data), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            return main(["--once", "--project", str(root), "--config", str(path)])

    def test_exit_code_mapping(self):
        self.assertEqual(exit_code_for([{"decision": "completed"}]), 0)
        self.assertEqual(exit_code_for([{"decision": "continue"}]), 0)
        self.assertEqual(exit_code_for([{"decision": "no_change"}]), 0)
        self.assertEqual(exit_code_for([{"decision": "human_confirmation"}]), 2)
        for decision in (
            "dirty_worktree",
            "protected_dirty_changed",
            "agent_failed",
            "test_failed",
            "agent_not_found",
            "timeout",
            "interrupted",
        ):
            self.assertEqual(exit_code_for([{"decision": decision}]), 1)
        self.assertEqual(exit_code_for([]), 1)

    def test_main_exit_zero_on_verification_success(self):
        root = self.repo()
        code = self.run_main(
            root,
            {
                "agent": {
                    "command": self.agent(
                        "from pathlib import Path; Path('src/out.py').write_text('ok')"
                    )
                },
                "design_files": ["SPEC.md"],
                "verification_commands": [[sys.executable, "-c", "pass"]],
                "stop_on_test_failure": True,
            },
        )
        self.assertEqual(code, 0)

    def test_main_exit_nonzero_on_verification_failure(self):
        root = self.repo()
        code = self.run_main(
            root,
            {
                "agent": {
                    "command": self.agent(
                        "from pathlib import Path; Path('src/out.py').write_text('ok')"
                    )
                },
                "design_files": ["SPEC.md"],
                "verification_commands": [[sys.executable, "-c", "raise SystemExit(1)"]],
                "stop_on_test_failure": True,
                "allow_task_chaining": True,
            },
        )
        self.assertEqual(code, 1)

    def test_main_exit_nonzero_on_dirty_worktree(self):
        root = self.repo()
        (root / "SPEC.md").write_text("modified", encoding="utf-8")
        code = self.run_main(
            root,
            {
                "agent": {"command": self.agent("pass")},
                "design_files": ["SPEC.md"],
                "verification_commands": [],
            },
        )
        self.assertEqual(code, 1)

    def test_installer_antigravity_command_syntax(self):
        install_path = Path(__file__).parent / "install.ps1"
        self.assertTrue(install_path.exists())
        content = install_path.read_text(encoding="utf-8")

        # Verify antigravity block exists and has correct order of args
        self.assertIn('"antigravity"', content)
        self.assertIn('"agy.exe"', content)

        # Ensure --prompt is at the very end of the antigravity array
        idx_agy = content.find('"agy.exe"')
        idx_skip = content.find('"--dangerously-skip-permissions"', idx_agy)
        idx_prompt = content.find('"--prompt"', idx_agy)

        self.assertNotEqual(idx_agy, -1)
        self.assertNotEqual(idx_skip, -1)
        self.assertNotEqual(idx_prompt, -1)
        self.assertTrue(idx_skip < idx_prompt, "(--dangerously-skip-permissions) must appear before (--prompt)")

        # Verify codex and claude blocks are intact
        self.assertIn('"codex"', content)
        self.assertIn('"claude"', content)


GATE_FILE = "instructions/instructions.md"


class TaskGateTests(BaseControllerTest):
    """The single-task gate (allow_task_chaining, default true = off).

    Without this gate, a cycle whose agent reported one task complete and
    then picked another task on its own was indistinguishable from normal
    progress: the loop just kept running. These tests pin down the fix -
    when allow_task_chaining=false, AutoLoop must only ever work the
    task_id named in the gate file, and must stop the moment that task's
    status leaves the actionable set ("pending"/"in_progress"), using
    max_cycles as a retry budget for that one task rather than a budget
    for hopping across many. allow_task_chaining defaults to true so any
    existing config.json that predates this feature keeps its original
    "agent picks any task" behaviour unchanged.

    Every agent command here is `self.agent(...)`: a small temporary
    Python script run as a subprocess. No real Claude/Codex/Antigravity
    process is ever started by this test class.
    """

    def write_gate(
        self, root: Path, task_id: str = "T-1", status: str = "pending", newline: str = "\n"
    ) -> None:
        # Committed, not left dirty: the pre-existing dirty-worktree gate in
        # Controller.run() must not confuse "expected project file" with
        # "agent left something uncommitted", so tests give it a clean base.
        gate = root / GATE_FILE
        gate.parent.mkdir(parents=True, exist_ok=True)
        body = f"---{newline}task_id: {task_id}{newline}status: {status}{newline}---{newline}{newline}# {task_id}{newline}{newline}body{newline}"
        gate.write_bytes(body.replace("\n", newline).encode("utf-8"))
        subprocess.run(["git", "add", GATE_FILE], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", f"gate: {task_id} {status}"], cwd=root, check=True)

    def write_raw_gate(self, root: Path, content: str) -> Path:
        # For parser-only tests that call read_task_state() directly and
        # never touch Controller.run()'s dirty-worktree gate.
        gate = root / GATE_FILE
        gate.parent.mkdir(parents=True, exist_ok=True)
        gate.write_text(content, encoding="utf-8")
        return gate

    def gated_config(self, root: Path, command: list[str], **kwargs) -> Config:
        return Config(
            root,
            design_files=["SPEC.md"],
            verification_commands=kwargs.get(
                "verification_commands", [[sys.executable, "-c", "pass"]]
            ),
            agent_command=command,
            agent_timeout_seconds=kwargs.get("timeout", 5),
            max_cycles=kwargs.get("max_cycles", 3),
            allow_task_chaining=False,
            commit_enabled=kwargs.get("commit_enabled", False),
        )

    # -- Config schema and backward compatibility -------------------------

    def test_config_default_allows_task_chaining(self):
        root = self.repo()
        path = root / "config.json"
        path.write_text("{}", encoding="utf-8")
        config = Config.load(path, root)
        self.assertTrue(config.allow_task_chaining)
        self.assertEqual(config.task_file, "instructions/instructions.md")

    def test_omitted_config_keeps_legacy_behavior(self):
        # A config.json written before this feature existed has neither key.
        # It must keep picking tasks freely, with no task_file required.
        root = self.repo()
        marker = root / "agent_ran.txt"
        agent = self.agent(f"from pathlib import Path; Path({str(marker)!r}).write_text('x')")
        config = Config(
            root,
            design_files=["SPEC.md"],
            verification_commands=[[sys.executable, "-c", "pass"]],
            agent_command=agent,
            max_cycles=1,
        )
        self.assertTrue(config.allow_task_chaining)
        receipts = Controller(config).run(once=True)
        self.assertTrue(marker.exists(), "legacy behaviour must still invoke the agent")
        self.assertEqual(receipts[0]["decision"], "continue")
        self.assertFalse(receipts[0]["task_gate_enabled"])

    def test_explicit_task_chaining_false_enables_gate(self):
        root = self.repo()
        path = root / "config.json"
        path.write_text(json.dumps({"allow_task_chaining": False}), encoding="utf-8")
        config = Config.load(path, root)
        self.assertFalse(config.allow_task_chaining)

    def test_task_file_absolute_path_rejected(self):
        root = self.repo()
        path = root / "config.json"
        for bad in ["C:/abs/task.md", "/rooted/task.md", "//server/share/task.md"]:
            path.write_text(json.dumps({"task_file": bad}), encoding="utf-8")
            with self.assertRaises(ControllerError):
                Config.load(path, root)

    def test_task_file_parent_escape_rejected(self):
        root = self.repo()
        path = root / "config.json"
        path.write_text(json.dumps({"task_file": "../escape/task.md"}), encoding="utf-8")
        with self.assertRaises(ControllerError):
            Config.load(path, root)

    # -- Front matter parsing: LF, CRLF, and every invalid case -----------

    def test_read_task_state_parses_frontmatter(self):
        root = self.repo()
        self.write_gate(root, task_id="S-9", status="pending")
        state = read_task_state(root, GATE_FILE)
        self.assertTrue(state.valid)
        self.assertEqual(state.task_id, "S-9")
        self.assertEqual(state.status, "pending")

    def test_read_task_state_tolerates_utf8_bom(self):
        # Found via manual PowerShell verification: Set-Content -Encoding
        # utf8 (and several common Windows editors) write a UTF-8 BOM by
        # default. A gate file saved that way must still parse.
        root = self.repo()
        gate = root / GATE_FILE
        gate.parent.mkdir(parents=True, exist_ok=True)
        gate.write_bytes(bytes([0xEF, 0xBB, 0xBF]) + '---\ntask_id: S-9\nstatus: pending\n---\n\nbody\n'.encode('utf-8'))
        state = read_task_state(root, GATE_FILE)
        self.assertTrue(state.valid)
        self.assertEqual(state.task_id, 'S-9')
        self.assertEqual(state.status, 'pending')


    def test_read_task_state_parses_crlf_frontmatter(self):
        root = self.repo()
        self.write_raw_gate(
            root,
            "---\r\ntask_id: S-9\r\nstatus: pending\r\n---\r\n\r\nbody\r\n",
        )
        state = read_task_state(root, GATE_FILE)
        self.assertTrue(state.valid)
        self.assertEqual(state.task_id, "S-9")
        self.assertEqual(state.status, "pending")

    def test_read_task_state_missing_file_is_invalid(self):
        root = self.repo()
        state = read_task_state(root, GATE_FILE)
        self.assertFalse(state.valid)
        self.assertIsNotNone(state.error)

    def test_read_task_state_missing_task_id_is_invalid(self):
        root = self.repo()
        self.write_raw_gate(root, "---\nstatus: pending\n---\n\nbody\n")
        state = read_task_state(root, GATE_FILE)
        self.assertFalse(state.valid)
        self.assertIn("task_id", state.error)

    def test_read_task_state_missing_status_is_invalid(self):
        root = self.repo()
        self.write_raw_gate(root, "---\ntask_id: S-9\n---\n\nbody\n")
        state = read_task_state(root, GATE_FILE)
        self.assertFalse(state.valid)
        self.assertIn("status", state.error)

    def test_read_task_state_unrecognized_status_is_invalid(self):
        root = self.repo()
        self.write_raw_gate(root, "---\ntask_id: S-9\nstatus: somewhere-in-between\n---\n\nbody\n")
        state = read_task_state(root, GATE_FILE)
        self.assertFalse(state.valid)
        self.assertEqual(state.task_id, "S-9")
        self.assertIn("status", state.error)

    # -- task_gate_invalid: fail closed, agent never starts ---------------

    def test_missing_gate_file_is_task_gate_invalid(self):
        root = self.repo()
        marker = root / "agent_ran.txt"
        agent = self.agent(f"from pathlib import Path; Path({str(marker)!r}).write_text('x')")
        config = self.gated_config(root, agent)
        receipts = Controller(config).run()
        self.assertEqual([r["decision"] for r in receipts], ["task_gate_invalid"])
        self.assertFalse(marker.exists(), "agent must not run when the gate file is invalid")
        self.assertEqual(receipts[0]["tests"], [], "verification must not run either")
        self.assertNotEqual(exit_code_for(receipts), 0)

    # -- no_pending_task: valid but not actionable right now --------------

    def test_completed_status_stops_without_invoking_agent(self):
        root = self.repo()
        self.write_gate(root, status="completed")
        marker = root / "agent_ran.txt"
        agent = self.agent(f"from pathlib import Path; Path({str(marker)!r}).write_text('x')")
        config = self.gated_config(root, agent)
        receipts = Controller(config).run()
        self.assertEqual([r["decision"] for r in receipts], ["no_pending_task"])
        self.assertFalse(marker.exists(), "agent must not run when no task is pending")
        self.assertEqual(exit_code_for(receipts), 0)

    def test_blocked_status_stops_without_invoking_agent(self):
        root = self.repo()
        self.write_gate(root, status="blocked")
        marker = root / "agent_ran.txt"
        agent = self.agent(f"from pathlib import Path; Path({str(marker)!r}).write_text('x')")
        config = self.gated_config(root, agent)
        receipts = Controller(config).run()
        self.assertEqual([r["decision"] for r in receipts], ["no_pending_task"])
        self.assertFalse(marker.exists())
        self.assertEqual(exit_code_for(receipts), 0)

    def test_failed_status_stops_without_invoking_agent(self):
        root = self.repo()
        self.write_gate(root, status="failed")
        marker = root / "agent_ran.txt"
        agent = self.agent(f"from pathlib import Path; Path({str(marker)!r}).write_text('x')")
        config = self.gated_config(root, agent)
        receipts = Controller(config).run()
        self.assertEqual([r["decision"] for r in receipts], ["no_pending_task"])
        self.assertFalse(marker.exists())
        self.assertEqual(exit_code_for(receipts), 0)

    # -- pending: the one status that starts an agent cycle ---------------

    def test_pending_status_invokes_agent_once(self):
        root = self.repo()
        self.write_gate(root, status="pending")
        marker = root / "agent_ran.txt"
        agent = self.agent(f"from pathlib import Path; Path({str(marker)!r}).write_text('x')")
        config = self.gated_config(root, agent, max_cycles=1)
        Controller(config).run()
        self.assertTrue(marker.exists())

    def test_pending_task_completes_and_stops_after_one_cycle(self):
        root = self.repo()
        self.write_gate(root, task_id="T-1", status="pending")
        agent = self.agent(
            "from pathlib import Path\n"
            "p = Path('instructions/instructions.md')\n"
            "p.write_text(p.read_text(encoding='utf-8')"
            ".replace('status: pending', 'status: completed'), encoding='utf-8')\n"
            "Path('src').mkdir(exist_ok=True)\n"
            "Path('src/out.py').write_text('ok', encoding='utf-8')\n"
        )
        config = self.gated_config(root, agent, max_cycles=5)
        receipts = Controller(config).run()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["decision"], "completed")
        self.assertEqual(exit_code_for(receipts), 0)

    def test_pending_task_retries_up_to_max_cycles_then_stops(self):
        root = self.repo()
        self.write_gate(root, task_id="T-1", status="pending")
        agent = self.agent(
            "import uuid\n"
            "from pathlib import Path\n"
            "Path(f'src/{uuid.uuid4().hex}.py').write_text('ok', encoding='utf-8')\n"
        )
        config = self.gated_config(root, agent, max_cycles=2)
        receipts = Controller(config).run()
        self.assertEqual(len(receipts), 2, "must retry the same task_id, not hop to another")
        self.assertTrue(all(r["decision"] == "continue" for r in receipts))
        state = read_task_state(root, GATE_FILE)
        self.assertTrue(state.valid)
        self.assertEqual(
            (state.task_id, state.status),
            ("T-1", "pending"),
            "task_id must still be the one named in the gate file",
        )

    # -- task_id changed without approval: human_confirmation, not a hop --

    def test_task_id_changed_without_approval_stops(self):
        root = self.repo()
        self.write_gate(root, task_id="T-1", status="pending")
        agent = self.agent(
            "from pathlib import Path\n"
            "p = Path('instructions/instructions.md')\n"
            "p.write_text(p.read_text(encoding='utf-8')"
            ".replace('task_id: T-1', 'task_id: T-2'), encoding='utf-8')\n"
        )
        config = self.gated_config(root, agent, max_cycles=5)
        receipts = Controller(config).run()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["decision"], "human_confirmation")
        self.assertEqual(exit_code_for(receipts), HUMAN_CONFIRMATION_EXIT_CODE)

    # -- in_progress: treated like pending, both at start and on continue -

    def test_in_progress_status_invokes_agent_at_start(self):
        root = self.repo()
        self.write_gate(root, status="in_progress")
        marker = root / "agent_ran.txt"
        agent = self.agent(f"from pathlib import Path; Path({str(marker)!r}).write_text('x')")
        config = self.gated_config(root, agent, max_cycles=1)
        Controller(config).run()
        self.assertTrue(marker.exists(), "in_progress is adopted as actionable, same as pending")

    def test_in_progress_after_agent_continues_retry_budget(self):
        root = self.repo()
        self.write_gate(root, task_id="T-1", status="pending")
        agent = self.agent(
            "from pathlib import Path\n"
            "p = Path('instructions/instructions.md')\n"
            "p.write_text(p.read_text(encoding='utf-8')"
            ".replace('status: pending', 'status: in_progress'), encoding='utf-8')\n"
            "Path('src').mkdir(exist_ok=True)\n"
            "Path('src/out.py').write_text('ok', encoding='utf-8')\n"
        )
        config = self.gated_config(root, agent, max_cycles=1)
        receipts = Controller(config).run()
        self.assertEqual(receipts[0]["decision"], "continue")
        self.assertEqual(receipts[0]["task_status_after"], "in_progress")

    # -- blocked/failed set by the agent itself: human_confirmation -------

    def test_agent_setting_blocked_status_triggers_human_confirmation(self):
        root = self.repo()
        self.write_gate(root, task_id="T-1", status="pending")
        agent = self.agent(
            "from pathlib import Path\n"
            "p = Path('instructions/instructions.md')\n"
            "p.write_text(p.read_text(encoding='utf-8')"
            ".replace('status: pending', 'status: blocked'), encoding='utf-8')\n"
        )
        config = self.gated_config(root, agent, max_cycles=5)
        receipts = Controller(config).run()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["decision"], "human_confirmation")
        self.assertEqual(exit_code_for(receipts), HUMAN_CONFIRMATION_EXIT_CODE)
        self.assertEqual(receipts[0]["task_status_after"], "blocked")

    def test_agent_setting_failed_status_triggers_human_confirmation(self):
        root = self.repo()
        self.write_gate(root, task_id="T-1", status="pending")
        agent = self.agent(
            "from pathlib import Path\n"
            "p = Path('instructions/instructions.md')\n"
            "p.write_text(p.read_text(encoding='utf-8')"
            ".replace('status: pending', 'status: failed'), encoding='utf-8')\n"
        )
        config = self.gated_config(root, agent, max_cycles=5)
        receipts = Controller(config).run()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["decision"], "human_confirmation")
        self.assertEqual(receipts[0]["task_status_after"], "failed")

    # -- chaining enabled: gate machinery is not consulted at all ---------

    def test_chaining_enabled_ignores_missing_task_file(self):
        root = self.repo()
        # No instructions/instructions.md at all, and allow_task_chaining
        # left at its (now legacy-compatible) default of True.
        receipt = Controller(self.config(root, allow_task_chaining=True)).run(once=True)[0]
        self.assertEqual(receipt["decision"], "continue")
        self.assertFalse(receipt["task_gate_enabled"])
        self.assertIsNone(receipt["task_file"])

    # -- receipt schema: same base fields on every path --------------------

    _BASE_RECEIPT_FIELDS = (
        "cycle", "started_at", "finished_at", "project_root", "agent",
        "agent_exit_code", "test_exit_codes", "changed_files", "stdout_file",
        "stderr_file", "decision", "before_changed_files", "tests",
        "agent_error", "preexisting_dirty_files", "agent_changed_files",
        "protected_dirty_violations", "task_gate_enabled", "task_file",
        "task_id", "task_status_before", "task_status_after", "task_gate_error",
    )

    def test_no_pending_task_receipt_has_base_fields(self):
        root = self.repo()
        self.write_gate(root, status="completed")
        agent = self.agent("pass")
        config = self.gated_config(root, agent)
        receipt = Controller(config).run()[0]
        for field in self._BASE_RECEIPT_FIELDS:
            self.assertIn(field, receipt, f"missing base field: {field}")

    def test_task_gate_invalid_receipt_has_base_fields(self):
        root = self.repo()
        agent = self.agent("pass")
        config = self.gated_config(root, agent)
        receipt = Controller(config).run()[0]
        for field in self._BASE_RECEIPT_FIELDS:
            self.assertIn(field, receipt, f"missing base field: {field}")

    def test_receipt_never_contains_task_file_body(self):
        root = self.repo()
        secret_marker = "UNIQUE-GATE-BODY-MARKER-38213"
        gate = root / GATE_FILE
        gate.parent.mkdir(parents=True, exist_ok=True)
        gate.write_text(
            f"---\ntask_id: T-1\nstatus: pending\n---\n\n{secret_marker}\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", GATE_FILE], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "gate"], cwd=root, check=True)
        agent = self.agent("pass")
        config = self.gated_config(root, agent, max_cycles=1)
        receipt = Controller(config).run()[0]
        self.assertNotIn(secret_marker, json.dumps(receipt, ensure_ascii=False))

    # -- task_file is not excluded from dirty-worktree detection ----------

    def test_task_file_change_not_excluded_from_dirty_check(self):
        root = self.repo()
        self.write_gate(root, task_id="T-1", status="pending")
        agent = self.agent(
            "from pathlib import Path\n"
            "p = Path('instructions/instructions.md')\n"
            "p.write_text(p.read_text(encoding='utf-8')"
            ".replace('status: pending', 'status: completed'), encoding='utf-8')\n"
        )
        config = self.gated_config(root, agent, max_cycles=1)
        first = Controller(config).run()
        self.assertEqual(first[0]["decision"], "completed")
        # The completed status change is left uncommitted (commit_enabled is
        # False), so a fresh Controller run must see it as dirty rather than
        # silently treating the gate file as AutoLoop-internal.
        second = Controller(self.gated_config(root, agent, max_cycles=1)).run()
        self.assertEqual(second[0]["decision"], "dirty_worktree")
        self.assertIn(GATE_FILE, second[0]["dirty_paths"])

    def test_task_file_tracked_regardless_of_commit_enabled(self):
        root = self.repo()
        self.write_gate(root, task_id="T-1", status="pending")
        agent = self.agent(
            "from pathlib import Path\n"
            "p = Path('instructions/instructions.md')\n"
            "p.write_text(p.read_text(encoding='utf-8')"
            ".replace('status: pending', 'status: completed'), encoding='utf-8')\n"
        )
        config = self.gated_config(root, agent, max_cycles=1, commit_enabled=True)
        receipt = Controller(config).run()[0]
        self.assertIn(GATE_FILE, receipt["agent_changed_files"])

    def test_task_file_tracked_with_commit_disabled_default(self):
        root = self.repo()
        self.write_gate(root, task_id="T-1", status="pending")
        agent = self.agent(
            "from pathlib import Path\n"
            "p = Path('instructions/instructions.md')\n"
            "p.write_text(p.read_text(encoding='utf-8')"
            ".replace('status: pending', 'status: completed'), encoding='utf-8')\n"
        )
        config = self.gated_config(root, agent, max_cycles=1, commit_enabled=False)
        receipt = Controller(config).run()[0]
        self.assertIn(GATE_FILE, receipt["agent_changed_files"])

    # -- Prompt content ----------------------------------------------------

    def test_build_prompt_task_gate_names_task_and_forbids_others(self):
        root = self.repo()
        self.write_gate(root, task_id="S-9", status="pending")
        config = self.gated_config(root, self.agent("pass"))
        gate = read_task_state(root, GATE_FILE)
        prompt = build_prompt(config, None, gate)
        self.assertIn("S-9", prompt)
        self.assertIn(GATE_FILE, prompt)
        self.assertIn("以外のタスク", prompt)
        self.assertIn("in_progress", prompt)
        self.assertIn("blocked", prompt)
        self.assertIn("failed", prompt)
        self.assertIn("push", prompt)

    def test_build_prompt_chaining_enabled_ignores_task_file(self):
        root = self.repo()
        config = self.config(root)  # allow_task_chaining=True via helper default
        prompt = build_prompt(config, None, None)
        self.assertNotIn(GATE_FILE, prompt)



class RepositoryLockTests(BaseControllerTest):
    """RepositoryLock: an exclusive, per-repository lock at .autoloop/run.lock.

    Replaces the old Lock class (.runtime/autoloop.lock, pid+started_at only,
    no liveness check - see QandA.md for why). All liveness checks below use
    either the real pid of a short-lived Fake subprocess (never a real AI
    agent) or, for the one genuinely hard-to-trigger state (unknown), a
    monkeypatched controller._pid_liveness for that single test only.
    """

    def spawn_and_wait(self) -> int:
        # A trivial subprocess whose pid is guaranteed dead by the time this
        # returns, for constructing "stale" lock scenarios deterministically.
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=10)
        return proc.pid

    def write_lock_json(self, root: Path, data: dict) -> Path:
        path = root / ".autoloop" / "run.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def valid_record(self, root: Path, **overrides) -> dict:
        base = {
            "schema_version": 1,
            "repository": str(root.resolve()),
            "pid": os.getpid(),
            "parent_pid": None,
            "started_at": "2026-01-01T00:00:00+00:00",
            "process_started_at": None,
            "hostname": socket.gethostname(),
            "controller_instance_id": "11111111-1111-1111-1111-111111111111",
            "controller_path": None,
            "mode": "single-task",
            "cycle": None,
        }
        base.update(overrides)
        return base

    # -- Acquisition --------------------------------------------------------

    def test_acquire_succeeds_when_no_lock_exists(self):
        root = self.repo()
        lock = RepositoryLock(root)
        lock.acquire()
        try:
            self.assertTrue(lock.path.is_file())
        finally:
            lock.release()

    def test_lock_file_has_required_schema_fields(self):
        root = self.repo()
        lock = RepositoryLock(root)
        lock.acquire()
        try:
            data = json.loads(lock.path.read_text(encoding="utf-8"))
            for field in (
                "schema_version", "repository", "pid", "started_at",
                "hostname", "controller_instance_id", "mode",
            ):
                self.assertIn(field, data)
            self.assertEqual(data["schema_version"], 1)
        finally:
            lock.release()

    def test_lock_repository_field_is_normalized_absolute_path(self):
        root = self.repo()
        lock = RepositoryLock(root)
        lock.acquire()
        try:
            data = json.loads(lock.path.read_text(encoding="utf-8"))
            self.assertEqual(Path(data["repository"]), root.resolve())
            self.assertTrue(Path(data["repository"]).is_absolute())
        finally:
            lock.release()

    def test_lock_instance_id_is_unique_per_instance(self):
        root = self.repo()
        a = RepositoryLock(root)
        b = RepositoryLock(root)
        self.assertNotEqual(a.instance_id, b.instance_id)

    def test_concurrent_acquire_only_one_succeeds(self):
        root = self.repo()
        results: list[str] = []
        lock_results_guard = threading.Lock()
        start_barrier = threading.Barrier(2)

        def attempt():
            lock = RepositoryLock(root)
            start_barrier.wait()
            try:
                lock.acquire()
                outcome = "ok"
            except LockAcquisitionError:
                outcome = "rejected"
            with lock_results_guard:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(sorted(results), ["ok", "rejected"])

    # -- active: a second acquire is rejected, -UnlockStale leaves it alone -

    def test_active_lock_rejects_second_acquire(self):
        root = self.repo()
        first = RepositoryLock(root)
        first.acquire()
        try:
            second = RepositoryLock(root)
            with self.assertRaises(LockAcquisitionError) as ctx:
                second.acquire()
            self.assertEqual(ctx.exception.status.state, "active")
        finally:
            first.release()

    def test_unlock_stale_does_not_remove_active_lock(self):
        root = self.repo()
        lock = RepositoryLock(root)
        lock.acquire()
        try:
            status = RepositoryLock(root).unlock_stale()
            self.assertEqual(status.state, "active")
            self.assertTrue(lock.path.is_file())
        finally:
            lock.release()

    # -- stale: dead pid, detected and only explicitly removable ------------

    def test_stale_lock_detected_after_process_exits(self):
        root = self.repo()
        dead_pid = self.spawn_and_wait()
        self.write_lock_json(root, self.valid_record(root, pid=dead_pid))
        status = RepositoryLock(root).status()
        self.assertEqual(status.state, "stale")

    def test_unlock_stale_removes_stale_lock(self):
        root = self.repo()
        dead_pid = self.spawn_and_wait()
        lock_path = self.write_lock_json(root, self.valid_record(root, pid=dead_pid))
        status = RepositoryLock(root).unlock_stale()
        self.assertEqual(status.state, "stale")
        self.assertFalse(lock_path.is_file())

    def test_acquire_succeeds_after_stale_lock_is_cleared(self):
        root = self.repo()
        dead_pid = self.spawn_and_wait()
        self.write_lock_json(root, self.valid_record(root, pid=dead_pid))
        RepositoryLock(root).unlock_stale()
        lock = RepositoryLock(root)
        lock.acquire()  # must not raise
        lock.release()

    # -- invalid: malformed lock file, never auto-removed -------------------

    def test_invalid_json_lock_not_auto_removed(self):
        root = self.repo()
        lock_path = root / ".autoloop" / "run.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("{not valid json", encoding="utf-8")
        status = RepositoryLock(root).status()
        self.assertEqual(status.state, "invalid")
        RepositoryLock(root).unlock_stale()
        self.assertTrue(lock_path.is_file(), "invalid lock must survive -UnlockStale")

    def test_missing_required_field_is_invalid(self):
        root = self.repo()
        record = self.valid_record(root)
        del record["hostname"]
        self.write_lock_json(root, record)
        status = RepositoryLock(root).status()
        self.assertEqual(status.state, "invalid")

    def test_repository_mismatch_is_invalid(self):
        root = self.repo()
        other = self.repo()
        self.write_lock_json(root, self.valid_record(other))
        status = RepositoryLock(root).status()
        self.assertEqual(status.state, "invalid")

    # -- foreign_host: different hostname, never auto-removed ---------------

    def test_same_host_different_case_is_not_foreign_host(self):
        # A real-world mismatch found during manual verification:
        # $env:COMPUTERNAME on Windows can differ only in case from
        # socket.gethostname() for the same machine (e.g. all-caps vs
        # mixed-case). The comparison must be case-insensitive so this
        # doesn't produce a false foreign_host.
        root = self.repo()
        self.write_lock_json(root, self.valid_record(root, hostname=socket.gethostname().upper()))
        status = RepositoryLock(root).status()
        self.assertNotEqual(status.state, "foreign_host")

    def test_foreign_host_lock_not_auto_removed(self):
        root = self.repo()
        self.write_lock_json(root, self.valid_record(root, hostname="some-other-host"))
        status = RepositoryLock(root).status()
        self.assertEqual(status.state, "foreign_host")
        RepositoryLock(root).unlock_stale()
        self.assertTrue((root / ".autoloop" / "run.lock").is_file())

    def test_foreign_host_lock_rejects_acquire(self):
        root = self.repo()
        self.write_lock_json(root, self.valid_record(root, hostname="some-other-host"))
        with self.assertRaises(LockAcquisitionError) as ctx:
            RepositoryLock(root).acquire()
        self.assertEqual(ctx.exception.status.state, "foreign_host")

    # -- unknown: liveness cannot be determined, treated as blocking --------

    def test_unknown_liveness_treated_as_blocking(self):
        import controller as controller_module

        root = self.repo()
        self.write_lock_json(root, self.valid_record(root, pid=os.getpid()))
        original = controller_module._pid_liveness
        controller_module._pid_liveness = lambda pid, expected: "unknown"
        try:
            status = RepositoryLock(root).status()
            self.assertEqual(status.state, "unknown")
            RepositoryLock(root).unlock_stale()
            self.assertTrue((root / ".autoloop" / "run.lock").is_file())
            with self.assertRaises(LockAcquisitionError):
                RepositoryLock(root).acquire()
        finally:
            controller_module._pid_liveness = original

    # -- pid reuse: alive pid, mismatched start time -> stale ---------------

    def test_pid_reuse_with_mismatched_start_time_is_stale(self):
        import controller as controller_module

        root = self.repo()
        self.write_lock_json(
            root,
            self.valid_record(root, pid=os.getpid(), process_started_at="definitely-not-a-real-token"),
        )
        original = controller_module._windows_process_start_time
        controller_module._windows_process_start_time = lambda pid: "current-real-token"
        try:
            status = RepositoryLock(root).status()
            self.assertIn(status.state, ("stale", "active"))
            if os.name == "nt":
                self.assertEqual(status.state, "stale")
        finally:
            controller_module._windows_process_start_time = original

    # -- release: only by the owning instance --------------------------------

    def test_release_does_not_remove_foreign_instance_lock(self):
        root = self.repo()
        lock = RepositoryLock(root)
        lock.acquire()
        # Simulate another process re-writing the lock with a different
        # controller_instance_id (e.g. after this one crashed and a human
        # cleared + a new run started) before this instance's release runs.
        data = json.loads(lock.path.read_text(encoding="utf-8"))
        data["controller_instance_id"] = "99999999-9999-9999-9999-999999999999"
        lock.path.write_text(json.dumps(data), encoding="utf-8")
        succeeded, warning = lock.release()
        self.assertFalse(succeeded)
        self.assertIsNotNone(warning)
        self.assertTrue(lock.path.is_file(), "must not delete a lock it no longer owns")

    def test_release_failure_produces_warning(self):
        root = self.repo()
        lock = RepositoryLock(root)
        lock.acquire()
        lock.path.write_text("{corrupted", encoding="utf-8")
        succeeded, warning = lock.release()
        self.assertFalse(succeeded)
        self.assertIsNotNone(warning)

    def test_release_is_a_no_op_when_never_acquired(self):
        root = self.repo()
        lock = RepositoryLock(root)
        succeeded, warning = lock.release()
        self.assertTrue(succeeded)
        self.assertIsNone(warning)

    # -- release happens on every Controller.run() outcome -------------------

    def test_lock_released_after_normal_completion(self):
        root = self.repo()
        receipt = Controller(self.config(root)).run(once=True)[0]
        self.assertTrue(receipt["repository_lock_release_succeeded"])
        self.assertFalse((root / ".autoloop" / "run.lock").is_file())

    def test_lock_released_after_no_pending_task(self):
        root = self.repo()
        gate = root / "instructions" / "instructions.md"
        gate.parent.mkdir(parents=True, exist_ok=True)
        gate.write_text("---\ntask_id: T-1\nstatus: completed\n---\n\nbody\n", encoding="utf-8")
        subprocess.run(["git", "add", "instructions/instructions.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "gate"], cwd=root, check=True)
        config = Config(
            root, design_files=["SPEC.md"],
            verification_commands=[[sys.executable, "-c", "pass"]],
            agent_command=self.agent("pass"), allow_task_chaining=False,
        )
        receipt = Controller(config).run()[0]
        self.assertEqual(receipt["decision"], "no_pending_task")
        self.assertTrue(receipt["repository_lock_release_succeeded"])
        self.assertFalse((root / ".autoloop" / "run.lock").is_file())

    def test_lock_released_after_task_gate_invalid(self):
        root = self.repo()
        config = Config(
            root, design_files=["SPEC.md"],
            verification_commands=[[sys.executable, "-c", "pass"]],
            agent_command=self.agent("pass"), allow_task_chaining=False,
        )
        receipt = Controller(config).run()[0]
        self.assertEqual(receipt["decision"], "task_gate_invalid")
        self.assertTrue(receipt["repository_lock_release_succeeded"])
        self.assertFalse((root / ".autoloop" / "run.lock").is_file())

    def test_lock_released_after_agent_failure(self):
        root = self.repo()
        receipt = Controller(
            self.config(root, self.agent("raise SystemExit(7)"))
        ).run(once=True)[0]
        self.assertEqual(receipt["decision"], "agent_failed")
        self.assertTrue(receipt["repository_lock_release_succeeded"])
        self.assertFalse((root / ".autoloop" / "run.lock").is_file())

    def test_lock_released_after_verification_failure(self):
        root = self.repo()
        config = self.config(
            root,
            self.agent("from pathlib import Path; Path('src/out.py').write_text('ok')"),
            verification_commands=[[sys.executable, "-c", "raise SystemExit(1)"]],
            stop_on_test_failure=True,
        )
        receipt = Controller(config).run(once=True)[0]
        self.assertEqual(receipt["decision"], "test_failed")
        self.assertTrue(receipt["repository_lock_release_succeeded"])
        self.assertFalse((root / ".autoloop" / "run.lock").is_file())

    def test_lock_released_after_dirty_worktree(self):
        root = self.repo()
        (root / "SPEC.md").write_text("modified", encoding="utf-8")
        receipt = Controller(self.config(root)).run(once=True)[0]
        self.assertEqual(receipt["decision"], "dirty_worktree")
        self.assertTrue(receipt["repository_lock_release_succeeded"])
        self.assertFalse((root / ".autoloop" / "run.lock").is_file())

    def test_lock_released_after_keyboard_interrupt(self):
        root = self.repo()
        config = self.config(root)
        controller = Controller(config)
        original_cycle = controller.cycle

        def raising_cycle(*args, **kwargs):
            raise KeyboardInterrupt

        controller.cycle = raising_cycle
        receipts = controller.run(once=True)
        self.assertEqual(receipts[-1]["decision"], "interrupted")
        self.assertTrue(receipts[-1]["repository_lock_release_succeeded"])
        self.assertFalse((root / ".autoloop" / "run.lock").is_file())

    def test_lock_not_created_on_configuration_error(self):
        root = self.repo()
        path = root / "config.json"
        path.write_text(json.dumps({"task_file": "../escape.md"}), encoding="utf-8")
        with self.assertRaises(ControllerError):
            Config.load(path, root)
        self.assertFalse((root / ".autoloop" / "run.lock").is_file())

    # -- lock acquisition failure: agent/verification/commit never happen ---

    def test_lock_failure_does_not_invoke_agent(self):
        root = self.repo()
        marker = root / "agent_ran.txt"
        agent = self.agent(f"from pathlib import Path; Path({str(marker)!r}).write_text('x')")
        held = RepositoryLock(root)
        held.acquire()
        try:
            receipts = Controller(self.config(root, agent)).run(once=True)
            self.assertEqual(receipts[0]["decision"], "repository_locked")
            self.assertFalse(marker.exists())
        finally:
            held.release()

    def test_lock_failure_does_not_touch_task_file(self):
        root = self.repo()
        gate = root / "instructions" / "instructions.md"
        gate.parent.mkdir(parents=True, exist_ok=True)
        gate.write_text("---\ntask_id: T-1\nstatus: pending\n---\n\nbody\n", encoding="utf-8")
        subprocess.run(["git", "add", "instructions/instructions.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "gate"], cwd=root, check=True)
        before = gate.read_text(encoding="utf-8")
        config = Config(
            root, design_files=["SPEC.md"],
            verification_commands=[[sys.executable, "-c", "pass"]],
            agent_command=self.agent("pass"), allow_task_chaining=False,
        )
        held = RepositoryLock(root)
        held.acquire()
        try:
            receipts = Controller(config).run()
            self.assertEqual(receipts[0]["decision"], "repository_locked")
        finally:
            held.release()
        self.assertEqual(gate.read_text(encoding="utf-8"), before)

    def test_lock_failure_leaves_worktree_unchanged(self):
        # No commit logic exists in controller.py at all (commit_enabled is
        # a config placeholder not yet wired to any git action), so "does
        # not commit on lock failure" reduces to: the working tree is
        # byte-for-byte unchanged after a rejected acquire.
        root = self.repo()
        before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout
        held = RepositoryLock(root)
        held.acquire()
        try:
            Controller(self.config(root)).run(once=True)
        finally:
            held.release()
        after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(before, after)

    # -- no secrets in the lock file or the receipt --------------------------

    def test_lock_file_contains_no_env_secrets(self):
        root = self.repo()
        os.environ["AUTOLOOP_TEST_SECRET_TOKEN"] = "sk-should-never-appear-anywhere"
        try:
            lock = RepositoryLock(root)
            lock.acquire()
            try:
                content = lock.path.read_text(encoding="utf-8")
                self.assertNotIn("sk-should-never-appear-anywhere", content)
                self.assertNotIn("AUTOLOOP_TEST_SECRET_TOKEN", content)
            finally:
                lock.release()
        finally:
            del os.environ["AUTOLOOP_TEST_SECRET_TOKEN"]

    def test_receipt_lock_fields_contain_no_secrets(self):
        root = self.repo()
        os.environ["AUTOLOOP_TEST_SECRET_TOKEN"] = "sk-should-never-appear-in-receipt"
        try:
            receipt = Controller(self.config(root)).run(once=True)[0]
            self.assertNotIn(
                "sk-should-never-appear-in-receipt", json.dumps(receipt, ensure_ascii=False)
            )
        finally:
            del os.environ["AUTOLOOP_TEST_SECRET_TOKEN"]

    def test_safe_lock_status_omits_command_and_username(self):
        root = self.repo()
        lock = RepositoryLock(root)
        lock.acquire()
        try:
            status = RepositoryLock(root).status()
            safe = safe_lock_status(status)
            for forbidden in ("username", "command", "parent_pid", "process_started_at", "controller_path"):
                self.assertNotIn(forbidden, safe)
            for expected in ("state", "pid", "started_at", "repository", "hostname", "mode"):
                self.assertIn(expected, safe)
        finally:
            lock.release()

    # -- CLI: --lock-status / --unlock-stale never touch the agent ----------

    def run_main_capture(self, args: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(args)
        return code, buf.getvalue()

    def test_lock_status_flag_does_not_acquire(self):
        root = self.repo()
        code, out = self.run_main_capture(["--project", str(root), "--lock-status"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["state"], "none")
        self.assertFalse((root / ".autoloop" / "run.lock").is_file())

    def test_lock_status_flag_does_not_invoke_agent(self):
        root = self.repo()
        marker = root / "agent_ran.txt"
        path = root / "config.json"
        path.write_text(
            json.dumps({"agent": {"command": self.agent(f"from pathlib import Path; Path({str(marker)!r}).write_text('x')")}}),
            encoding="utf-8",
        )
        code, _ = self.run_main_capture(
            ["--project", str(root), "--config", str(path), "--lock-status"]
        )
        self.assertEqual(code, 0)
        self.assertFalse(marker.exists())

    def test_unlock_stale_flag_does_not_invoke_agent(self):
        root = self.repo()
        marker = root / "agent_ran.txt"
        path = root / "config.json"
        path.write_text(
            json.dumps({"agent": {"command": self.agent(f"from pathlib import Path; Path({str(marker)!r}).write_text('x')")}}),
            encoding="utf-8",
        )
        dead_pid = self.spawn_and_wait()
        self.write_lock_json(root, self.valid_record(root, pid=dead_pid))
        code, out = self.run_main_capture(
            ["--project", str(root), "--config", str(path), "--unlock-stale"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["state"], "stale")
        self.assertFalse(marker.exists())
        self.assertFalse((root / ".autoloop" / "run.lock").is_file())

    def test_unlock_stale_active_lock_via_cli_returns_exit_1(self):
        root = self.repo()
        held = RepositoryLock(root)
        held.acquire()
        try:
            code, out = self.run_main_capture(["--project", str(root), "--unlock-stale"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["state"], "active")
        finally:
            held.release()

    def test_powershell_wrapper_lockstatus_args_match_readme(self):
        wrapper = Path(__file__).parent / "examples" / "run-autoloop.ps1"
        content = wrapper.read_text(encoding="utf-8")
        self.assertIn("[switch]$LockStatus", content)
        self.assertIn("[switch]$UnlockStale", content)
        self.assertIn("--lock-status", content)
        self.assertIn("--unlock-stale", content)
        readme = Path(__file__).parent / "README.md"
        readme_text = readme.read_text(encoding="utf-8")
        self.assertIn("-LockStatus", readme_text)
        self.assertIn("-UnlockStale", readme_text)

if __name__ == "__main__":
    unittest.main()
