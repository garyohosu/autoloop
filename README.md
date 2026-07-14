# AutoLoop

AutoLoop is a lightweight Python controller that repeatedly runs an AI coding agent against a target Git repository until the project is complete or human input is genuinely required.

## What problem it solves

AI coding tools can write code, but a human still has to repeat the work around each coding session:

```text
Review the result
→ Check the tests
→ Select the next task
→ Write another prompt
→ Restart the agent
```

AutoLoop reduces that repetitive work. It asks the configured agent to inspect the current repository, select one small task, implement it, run fixed verification commands, save logs and a receipt, and continue to the next cycle.

## How it works

```text
Inspect the target repository
→ Select the next small task
→ Run an AI coding agent
→ Execute tests
→ Save logs and receipts
→ Continue until human input is truly required
```

AutoLoop is intentionally small. The current controller does not implement its own large planner. Task selection and safe, reversible technical decisions are delegated to the configured coding agent through the generated prompt.

## Repository separation

AutoLoop is stored separately from the projects it works on:

```text
C:\PROJECT\
├─ autoloop\
├─ private-project-a\
└─ private-project-b\
```

The target project keeps its own specifications, test commands, agent configuration, and runtime output. AutoLoop itself can remain public while target repositories remain private.

## Requirements

- Python 3.11 or later
- Git available on `PATH`
- A target Git repository
- A non-interactive coding-agent command configured for that target project

AutoLoop has no third-party Python dependencies.

## Target project setup

Create an `.autoloop` directory in the target repository:

```text
target-project/
├─ .autoloop/
│  ├─ config.json
│  ├─ local.json
│  └─ run-autoloop.ps1
├─ SPEC.md
└─ src/
```

Copy these files:

```text
config.example.json             → target-project/.autoloop/config.json
examples/local.example.json     → target-project/.autoloop/local.json
examples/run-autoloop.ps1       → target-project/.autoloop/run-autoloop.ps1
```

Edit `local.json` so that it points to the shared AutoLoop folder:

```json
{
  "autoloop_home": "C:\\PROJECT\\autoloop"
}
```

`local.json` is machine-specific and should normally be ignored by the target repository:

```gitignore
.autoloop/local.json
.runtime/
```

## Run from the target repository

One-cycle acceptance test:

```powershell
Set-Location C:\PROJECT\target-project
.\.autoloop\run-autoloop.ps1 -Once
```

Continuous mode:

```powershell
.\.autoloop\run-autoloop.ps1
```

You can also call the controller directly:

```powershell
py C:\PROJECT\autoloop\controller.py `
    --project C:\PROJECT\target-project `
    --config .autoloop\config.json `
    --once
```

A relative `--config` path is resolved from the target repository, not from the AutoLoop repository. If `--project` is omitted, AutoLoop uses the current Git repository. If `--config` is omitted, it uses `.autoloop/config.json` in the target repository.

## Configuration

The agent is selected entirely through `agent.command`:

```json
{
  "agent": {
    "name": "codex",
    "command": ["codex", "exec", "--sandbox", "workspace-write"],
    "prompt_delivery": "argument",
    "timeout_seconds": 1800
  }
}
```

`prompt_delivery` supports:

- `argument`: append the generated prompt as the final command-line argument
- `stdin`: send the generated prompt through standard input

This allows the same controller to work with different coding agents when they provide a non-interactive CLI.

## Runtime output

Runtime files are written to the target project, not to the AutoLoop repository:

```text
target-project/.runtime/
├─ autoloop.lock
├─ logs/
│  └─ cycle-001/
└─ receipts/
   └─ cycle-001.json
```

Each receipt records the target project, agent exit code, verification exit codes, changed files, log paths, and the controller decision.

## Stop conditions

The controller stops when the agent reports project completion or human confirmation, when an agent failure requires stopping, when repeated cycles make no changes, when the maximum cycle count is reached, or when the user interrupts the process.

The generated prompt instructs the agent to make technical and reversible decisions automatically, while requiring human confirmation for destructive or irreversible operations, credentials, money, external publication, privacy, legal matters, fundamental product decisions, or cases without a safe default.

## Safety

- AutoLoop does not commit or push.
- Agent commands are configured separately for each target project.
- Commands are executed with `shell=False`.
- Verification commands are fixed argv lists from the target configuration.
- Logs and receipts remain in the target project.
- Start with `--once` before using continuous mode.
- Uncommitted changes are never stashed, reset, or deleted by AutoLoop.

The configured agent can still modify files within its own permission boundary. Review the agent command and sandbox settings before using AutoLoop on important repositories.

### Dirty worktree protection

A dirty worktree is rejected by default. If the target repository contains any tracked or untracked changes (outside `.runtime/`), AutoLoop stops with decision `dirty_worktree` before starting the agent and reports the detected paths on stderr and in the result JSON. Do not work around this by stashing, resetting, or committing existing changes — run the first cycle in a clean dedicated test repository or a fresh `git worktree` instead. This also applies to AI CLI agents operating AutoLoop: never continue unconditionally on a dirty worktree.

`allow_dirty_worktree` is an explicit setting for advanced use:

```json
{
  "allow_dirty_worktree": false,
  "allowed_dirty_paths": []
}
```

Even with `"allow_dirty_worktree": true`, only paths listed in `allowed_dirty_paths` may be dirty. Entries must be relative paths inside the target Git root; absolute paths, `..`, and UNC paths are rejected. Any dirty path not covered by the list still stops the run.

Pre-existing dirty files are protected by hashing. Before the agent runs, AutoLoop records each allowed dirty file (git status, file type, existence, size, SHA-256, stage state, rename information). After the agent and verification finish, the state is captured again; if any protected file was modified, deleted, renamed, staged, or unstaged, AutoLoop stops with decision `protected_dirty_changed`. Receipts record `preexisting_dirty_files`, `agent_changed_files`, and `protected_dirty_violations` separately, so agent-created changes are distinguished from the user's pre-existing changes.

### Cycle numbering

Cycle numbers continue from the existing runtime history: the next cycle is one greater than the highest valid `cycle-NNN` found under `.runtime/logs/` and `.runtime/receipts/` (invalid names such as `cycle-test` or `backup-cycle-001.json` are ignored). Existing logs and receipts are never overwritten; if a cycle directory or receipt already exists at the chosen number, AutoLoop stops safely with an error.

### Exit codes

The controller process exit code reflects the final decision:

| Final decision | Process exit code |
|---|---|
| `completed`, `continue`, `no_change` | 0 |
| `human_confirmation` | 2 |
| `dirty_worktree`, `protected_dirty_changed`, `agent_failed`, `test_failed`, `agent_not_found`, `timeout`, other errors | 1 |

`human_confirmation` uses a dedicated exit code (2) so callers can distinguish "needs a human decision" from real failures.

## Tests

```powershell
py -m unittest -v controller_tests.py
py -m py_compile controller.py controller_tests.py
```

The tests use temporary Git repositories and fake agents, so they do not require Codex, Claude Code, or another paid service.

## Current status

This is an early Phase 1 implementation. It currently has:

- configurable agent commands
- argument or stdin prompt delivery
- one-cycle and multi-cycle execution
- fixed verification commands
- target-side logs, receipts, and locking

It does not currently provide:

- automatic fallback between coding agents
- automatic commit or push
- a GUI
- complete descendant-process cleanup after timeouts
- guaranteed compatibility with every agent CLI

## Design history

The Markdown design files in this repository record earlier and more ambitious designs considered during development. The current `controller.py` is intentionally smaller than some of those documents describe.

The Japanese development article is available in [`note.md`](note.md).

## License

No license has been selected yet.
