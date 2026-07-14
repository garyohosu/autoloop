import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from controller import (
    Config,
    Controller,
    ControllerError,
    Lock,
    VALID_PROMPT_DELIVERIES,
    build_prompt,
    exit_code_for,
    main,
    next_cycle_number,
    resolve_config_path,
    resolve_project_root,
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
        lock = Lock(root / ".runtime" / "autoloop.lock")
        lock.acquire()
        try:
            with self.assertRaises(ControllerError):
                Controller(config).run(once=True)
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


if __name__ == "__main__":
    unittest.main()
