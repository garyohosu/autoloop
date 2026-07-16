# AutoLoop

AutoLoop is a lightweight Python controller that repeatedly runs an AI coding agent against a target Git repository until the project is complete or human input is genuinely required.

## Instructions for AI coding agents

You are reading this README because the user wants to install or run AutoLoop in their target Git repository.

Follow these rules strictly:

- **No manual user actions**: Install and run AutoLoop automatically. Do not ask the user to copy files or edit JSON files unless the automatic setup completely fails.
- **Do not modify the AutoLoop controller**: Do not change the design or logic of `controller.py` or `controller_tests.py` during installation.
- **Run exactly one cycle**: By default, run only one cycle (`-Once`) to verify safety and configuration. Do not run in continuous mode unless the user explicitly requested it.
- **No auto-commit/push**: Do not commit or push any changes inside the target project unless the user explicitly requested it.
- **Do not touch uncommitted changes**: Do not delete, stash, or reset any existing uncommitted changes in the target project.
- **Never proceed unconditionally on a dirty worktree**: AutoLoop refuses to start the agent when the target repository has tracked or untracked changes (decision `dirty_worktree`, exit code 1). Do not work around this by stashing, resetting, or committing the user's changes. Run the first cycle in a clean dedicated test repository or a fresh `git worktree` instead, or ask the user before setting `allow_dirty_worktree`.

## Quick Start for AI CLI

To install and run AutoLoop in the current target Git repository, execute the following steps.

### Step 1: Verify the target repository

```powershell
git rev-parse --show-toplevel
git status --short
```

If the current directory is not a Git repository, stop and report the error. If there are uncommitted changes, report them to the user, but **do not** stash, delete, or reset them — AutoLoop will refuse to run on a dirty worktree by default, so use a clean dedicated test repository or a fresh `git worktree` for the first cycle instead of forcing execution.

### Step 2: Locate the AutoLoop repository

Search for the AutoLoop home directory (where `controller.py` and `install.ps1` reside) in the following order:

1. Environment variable `$env:AUTOLOOP_HOME`
2. `autoloop_home` in the target's `.autoloop/local.json` (if present)
3. Parent directory sibling (`../autoloop`)
4. Windows default path (`C:\PROJECT\autoloop`)

If it is not found in any of these places, clone it to a path outside the target repository:

```powershell
git clone https://github.com/garyohosu/autoloop.git C:\PROJECT\autoloop
```

If it already exists, do not clone it again. If you need to update it, verify it is clean and run `git pull --ff-only`.

### Step 3: Run the installer

Run the installer from the target repository root:

```powershell
& C:\PROJECT\autoloop\install.ps1 -Agent auto
```

`-Agent auto` detects an available agent CLI automatically. You may also specify one explicitly: `-Agent antigravity`, `-Agent codex`, or `-Agent claude`. On Windows, `-Agent codex` resolves the npm shim to a directly executable `node.exe` + `codex.js` command, because the controller starts agents with `shell=False`.

### Step 4: Verify the installation output

```powershell
Test-Path .\.autoloop\config.json
Test-Path .\.autoloop\local.json
Test-Path .\.autoloop\run-autoloop.ps1
```

If any of these are missing, abort and report the error. Adjust `verification_commands` in `.autoloop/config.json` if the target project uses a different test command than the generated default.

### Step 5: Run one cycle

```powershell
.\.autoloop\run-autoloop.ps1 -Once
```

Unless the user explicitly requests continuous execution, do not run without `-Once`.

### Step 6: Verify and report the results

```powershell
git status --short
Get-ChildItem .runtime -Recurse -ErrorAction SilentlyContinue
```

Review the newest receipt under `.runtime/receipts/` (cycle numbers continue from existing history) and report:

- Selected agent and the generated agent command
- Configuration file used
- Controller process exit code (0 = success, 2 = needs human confirmation, 1 = failure)
- Verification test results
- Changed files (`agent_changed_files` vs `preexisting_dirty_files`)
- Stop reason (`decision`)
- Whether it is safe to proceed to continuous mode

## Quick Start for humans

If you want your AI agent (Antigravity, Claude Code, Codex CLI, ...) to install and run AutoLoop, copy and paste the following prompt.

### Recommended prompt (Japanese)

```text
以下のREADMEを読んで、現在のGitリポジトリにAutoLoopを導入してください。

https://github.com/garyohosu/autoloop/blob/main/README.md

設定ファイルの生成、利用可能なAgentの選択、必要な事前確認を自動で行い、
最初は1サイクルだけ実行してください。

未コミットの変更を削除、stash、resetしないでください。
commitとpushも行わないでください。
最後に実行結果と停止理由を報告してください。
```

### Recommended prompt (English)

```text
Read the following README and install AutoLoop into the current Git repository:

https://github.com/garyohosu/autoloop/blob/main/README.md

Automatically detect the available coding-agent CLI, create the required
configuration files, perform the required safety checks, and run exactly one
AutoLoop cycle.

Do not delete, stash, or reset existing uncommitted changes.
Do not commit or push.
Report the execution result and stop reason when finished.
```

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

### Automatic setup (recommended)

Run the installer from the target repository root:

```powershell
Set-Location C:\PROJECT\target-project
& C:\PROJECT\autoloop\install.ps1 -Agent auto
```

The installer:

- verifies the target Git repository (and warns on a dirty worktree without touching it)
- detects an available agent CLI (`agy` → `codex` → `claude`) when `-Agent auto` is used
- generates `.autoloop/local.json` pointing at the AutoLoop home
- generates `.autoloop/config.json` for the selected agent (on Windows, `codex` is resolved to a `node.exe` + `codex.js` command that works with `shell=False`)
- copies `examples/run-autoloop.ps1` to `.autoloop/run-autoloop.ps1`
- appends `.autoloop/local.json` and `.runtime/` to the target `.gitignore` without duplicating existing rules

### Manual setup (alternative)

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

## Single-task gate (`allow_task_chaining`)

By default AutoLoop lets the agent pick any small task it likes each cycle (`allow_task_chaining: true`, the default, and the only behaviour before this feature existed). This kept existing `config.json` files working unchanged.

Set `allow_task_chaining: false` to restrict AutoLoop to exactly one task, named in a gate file (`task_file`, default `instructions/instructions.md`), instead of letting the agent choose freely. This closes a real gap: without the gate, a cycle where the agent finished one task and then quietly picked another looked identical to normal progress, so a run assigned to implement task S-9 could silently chain through S-10, T-3, and more.

```json
{
  "allow_task_chaining": false,
  "task_file": "instructions/instructions.md"
}
```

`task_file` must be a non-empty, relative path inside the target Git repository; an absolute path or a path that escapes the repository root with `..` is rejected at config-load time (`ControllerError`, agent never starts).

### Gate file front matter

The gate file's leading YAML-style front matter is the only thing AutoLoop reads:

```markdown
---
task_id: S-4
status: pending
---

# S-4

Task details for the agent go here.
```

`task_id` and `status` are required and must be non-empty. Recognized `status` values:

| status | Meaning | At cycle start | After the agent runs (same `task_id`) |
|---|---|---|---|
| `pending` | Approved, not started | Agent runs | Loop continues (retry budget) |
| `in_progress` | Approved, partially done | Agent runs (treated the same as `pending`) | Loop continues (retry budget) |
| `completed` | Done | Stops, `no_pending_task`, exit 0 | Stops, `completed`, exit 0 |
| `blocked` | Needs a human decision | Stops, `no_pending_task`, exit 0 | Stops, `human_confirmation`, exit 2 |
| `failed` | Attempted but did not succeed | Stops, `no_pending_task`, exit 0 | Stops, `human_confirmation`, exit 2 |

`in_progress` is deliberately adopted as an alias for `pending` in both places — see `QandA.md` for why. If you never use `in_progress`, nothing changes for you.

Any other problem — the file is missing, unreadable, has no parseable front matter, or `task_id`/`status` is missing, empty, or not one of the five values above — is `task_gate_invalid` (exit 1). The agent is never started and verification never runs. This also applies if the agent itself leaves the file broken (e.g. deletes it or writes an unrecognized status) after running.

If the agent changes `task_id` to something else without approval, that is `human_confirmation` (exit 2), not a hop to the new task — AutoLoop never auto-advances to a different `task_id` on its own. `max_cycles` is a retry budget for the *one* approved `task_id`, not a budget for working through many.

### `task_file` and dirty-worktree detection

`task_file` is **not** excluded from AutoLoop's dirty-worktree check (unlike `.autoloop/` and the runtime directory, which are AutoLoop's own bookkeeping). When the agent flips `status` to `completed` (or anything else) to signal the task is done, that edit is a real, meaningful change — losing track of it would let a stale task's actual state go unnoticed by whoever (human, or another agent) looks next.

With the default `commit_enabled: false`, AutoLoop does not commit that status change for you. That means after a cycle finishes, the repository is left dirty with just that one edit, and **the very next AutoLoop invocation will refuse to start** (`dirty_worktree`) until a human reviews and commits (or otherwise resolves) it — in addition to writing the next `task_id` into the gate file. Plan for both steps, not just picking the next task.

### Example: OracleCouncil

```json
{
  "allow_task_chaining": false,
  "task_file": "instructions/instructions.md"
}
```

```powershell
Set-Location C:\PROJECT\OracleCouncil
.\.autoloop\run-autoloop.ps1 -Once
```

### Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `task_gate_invalid` immediately, no agent output | `task_file` missing, malformed front matter, or an empty/unrecognized `task_id`/`status` | Open `task_file` and check the leading `---`/`---` block matches the format above |
| `no_pending_task`, exit 0, no agent output | `status` is `completed`/`blocked`/`failed` already | A human needs to write the next `task_id` and set `status: pending` |
| `dirty_worktree` right after a completed cycle | The previous cycle's `status: completed` edit to `task_file` was never committed (`commit_enabled: false`) | Review and commit (or otherwise resolve) that change before the next `task_id` |
| `human_confirmation`, exit 2, after a cycle | The agent changed `task_id`, or set `status` to `blocked`/`failed` | Read the receipt's `task_status_after` / `agent_error`, decide, then update `task_file` yourself |

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

A dirty worktree is rejected by default. If the target repository contains any tracked or untracked changes (outside AutoLoop's own `.runtime/` and `.autoloop/` directories), AutoLoop stops with decision `dirty_worktree` before starting the agent and reports the detected paths on stderr and in the result JSON. Freshly installed `.autoloop/` files therefore do not block the first cycle. Do not work around this by stashing, resetting, or committing existing changes — run the first cycle in a clean dedicated test repository or a fresh `git worktree` instead. This also applies to AI CLI agents operating AutoLoop: never continue unconditionally on a dirty worktree.

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
| `completed`, `continue`, `no_change`, `no_pending_task` | 0 |
| `human_confirmation` | 2 |
| `dirty_worktree`, `protected_dirty_changed`, `agent_failed`, `test_failed`, `agent_not_found`, `timeout`, `task_gate_invalid`, other errors | 1 |

`human_confirmation` uses a dedicated exit code (2) so callers can distinguish "needs a human decision" from real failures. `no_pending_task` and `task_gate_invalid` only occur when the single-task gate (`allow_task_chaining: false`) is enabled; see "Single-task gate" above.

## Tests

```powershell
py -m unittest -v controller_tests.py
py -m py_compile controller.py controller_tests.py
```

The tests use temporary Git repositories and fake agents, so they do not require Codex, Claude Code, or another paid service.

## Current status

This is an early Phase 1 implementation. It currently has:

- README-driven setup executable by AI CLI agents
- an installer (`install.ps1`) with agent auto-detection and config generation
- configurable agent commands
- argument or stdin prompt delivery
- one-cycle and multi-cycle execution
- fixed verification commands
- target-side logs, receipts, and locking
- dirty-worktree rejection and pre-existing dirty-file protection
- cycle numbering that continues from existing runtime history
- an opt-in single-task gate (`allow_task_chaining: false`) that restricts a run to one `task_id`

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
