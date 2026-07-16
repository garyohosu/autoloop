param(
    [ValidateSet("auto", "antigravity", "codex", "claude")]
    [string]$Agent = "auto"
)

$ErrorActionPreference = "Stop"

# --- Target repository (current directory) ---
$gitTop = git rev-parse --show-toplevel 2>$null
if ($LASTEXITCODE -ne 0 -or -not $gitTop) {
    Write-Error "The current directory is not a Git repository."
    exit 1
}
$ProjectRoot = (Get-Item $gitTop).FullName

# --- AutoLoop home (where this install.ps1 resides) ---
$AutoloopHome = $PSScriptRoot
if (-not $AutoloopHome) {
    $AutoloopHome = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$AutoloopHome = (Get-Item $AutoloopHome).FullName
if (-not (Test-Path (Join-Path $AutoloopHome "controller.py"))) {
    Write-Error "controller.py was not found next to install.ps1: $AutoloopHome"
    exit 1
}

# --- Dirty worktree notice (informational only; nothing is modified) ---
$dirty = git -C $ProjectRoot status --porcelain
if ($dirty) {
    Write-Warning "The target repository has uncommitted changes. AutoLoop refuses to run on a dirty worktree by default (decision 'dirty_worktree'). Run the first cycle in a clean test repository or a fresh 'git worktree'. Existing changes are left untouched."
}

# --- Agent selection ---
function Test-Cli([string]$Name) {
    [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

$SelectedAgent = $Agent.ToLower()
if ($SelectedAgent -eq "auto") {
    if ((Test-Cli "agy.exe") -or (Test-Cli "agy")) {
        $SelectedAgent = "antigravity"
    } elseif (Test-Cli "codex") {
        $SelectedAgent = "codex"
    } elseif (Test-Cli "claude") {
        $SelectedAgent = "claude"
    } else {
        Write-Error "No supported agent CLI (agy, codex, claude) was found on PATH."
        exit 1
    }
}

# --- Codex launch command ---
# The controller starts agents with shell=False, which cannot launch the npm
# shim scripts codex.ps1 / codex.cmd on Windows. Resolve node.exe + codex.js
# (machine-specific absolute paths generated at install time only).
function Resolve-CodexCommand {
    $suffix = @("exec", "--sandbox", "workspace-write")
    if ($env:OS -ne "Windows_NT") {
        return @("codex") + $suffix
    }

    $cmd = Get-Command codex -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -match "\.exe$") {
        return @($cmd.Source) + $suffix
    }

    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    $codexJs = $null
    try {
        $npmRoot = (& npm root -g 2>$null | Select-Object -First 1)
        if ($npmRoot) {
            $candidate = Join-Path $npmRoot "@openai/codex/bin/codex.js"
            if (Test-Path $candidate) { $codexJs = (Get-Item $candidate).FullName }
        }
    } catch {}
    if (-not $codexJs -and $cmd) {
        $candidate = Join-Path (Split-Path -Parent $cmd.Source) "node_modules/@openai/codex/bin/codex.js"
        if (Test-Path $candidate) { $codexJs = (Get-Item $candidate).FullName }
    }

    if ($node -and $codexJs) {
        return @($node.Source, $codexJs) + $suffix
    }
    if ($cmd) {
        Write-Warning "Could not resolve node.exe + codex.js. Falling back to '$($cmd.Source)'. If AutoLoop reports agent_not_found, set agent.command in .autoloop/config.json manually."
        return @($cmd.Source) + $suffix
    }
    Write-Error "codex CLI was not found on PATH."
    exit 1
}

switch ($SelectedAgent) {
    "antigravity" {
        $AgentCommand = @(
            "agy.exe", "--dangerously-skip-permissions",
            "--mode", "accept-edits", "--print-timeout", "30m",
            "--prompt"
        )
    }
    "codex" {
        $AgentCommand = Resolve-CodexCommand
    }
    "claude" {
        $AgentCommand = @("claude", "-p")
    }
}

Write-Host "Installing AutoLoop for agent: $SelectedAgent"

# --- Generate .autoloop ---
$AutoloopDir = Join-Path $ProjectRoot ".autoloop"
if (-not (Test-Path $AutoloopDir)) {
    New-Item -ItemType Directory -Path $AutoloopDir | Out-Null
}

[ordered]@{ autoloop_home = $AutoloopHome } |
    ConvertTo-Json |
    Set-Content -Path (Join-Path $AutoloopDir "local.json") -Encoding utf8

$Config = [ordered]@{
    agent = [ordered]@{
        name = $SelectedAgent
        command = $AgentCommand
        prompt_delivery = "argument"
        timeout_seconds = 1800
    }
    design_files = @(
        "SPEC.md", "USECASE.md", "SEQUENCE.md", "CLASS.md",
        "UI.md", "TESTCASE.md", "QandA.md"
    )
    verification_commands = @(, @("py", "-m", "unittest", "-q"))
    max_cycles = 10
    stop_on_agent_failure = $true
    stop_on_test_failure = $false
    commit_enabled = $false
    push_enabled = $false
    allow_dirty_worktree = $false
    allowed_dirty_paths = @()
    allow_task_chaining = $true
    task_file = "instructions/instructions.md"
}
$Config | ConvertTo-Json -Depth 6 |
    Set-Content -Path (Join-Path $AutoloopDir "config.json") -Encoding utf8

$SourceWrapper = Join-Path $AutoloopHome "examples\run-autoloop.ps1"
if (-not (Test-Path $SourceWrapper)) {
    Write-Error "run-autoloop.ps1 was not found: $SourceWrapper"
    exit 1
}
Copy-Item -Path $SourceWrapper -Destination (Join-Path $AutoloopDir "run-autoloop.ps1") -Force

# --- Update the target repository's .gitignore (append only, no duplicates) ---
$GitignorePath = Join-Path $ProjectRoot ".gitignore"
$IgnoreRules = @(".autoloop/local.json", ".autoloop/run.lock", ".runtime/")
$ExistingContent = ""
if (Test-Path $GitignorePath) {
    $ExistingContent = Get-Content -Path $GitignorePath -Raw
}
$NewRules = @()
foreach ($Rule in $IgnoreRules) {
    $EscapedRule = [regex]::Escape($Rule)
    if ($ExistingContent -notmatch "(?m)^$EscapedRule\s*$") {
        $NewRules += $Rule
    }
}
if ($NewRules.Count -gt 0) {
    if ($ExistingContent -and $ExistingContent -notmatch "\r?\n$") {
        Add-Content -Path $GitignorePath -Value ""
    }
    foreach ($Rule in $NewRules) {
        Add-Content -Path $GitignorePath -Value $Rule
    }
    Write-Host "Appended new ignore rules to .gitignore."
}

Write-Host "AutoLoop installation completed successfully."
Write-Host "Agent command: $($AgentCommand -join ' ')"
Write-Host "Next step: .\.autoloop\run-autoloop.ps1 -Once"
