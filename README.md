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

The configured agent can still modify files within its own permission boundary. Review the agent command and sandbox settings before using AutoLoop on important repositories.

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
