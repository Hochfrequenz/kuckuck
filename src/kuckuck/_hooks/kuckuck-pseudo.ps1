# Claude Code PreToolUse hook: auto-pseudonymize .eml/.msg files before
# Read / Edit / Grep. PowerShell counterpart to kuckuck-pseudo.sh, used
# on native Windows (without WSL).
#
# Contract (see https://code.claude.com/docs/en/hooks):
#   - stdin:  PreToolUse JSON payload; .tool_name holds the tool,
#             .tool_input.file_path (Read, Edit) or .tool_input.path (Grep)
#             holds the target path.
#   - exit 0: tool call proceeds.
#   - exit 2: tool call is blocked; stderr is fed back to the model.
#
# Environment:
#   KUCKUCK_HOOK_FAIL_OPEN=1
#     Debug-only escape hatch; missing kuckuck or a failing pseudonymization
#     will NOT block the tool call. UNSAFE - documented so it is easy to
#     switch on for local triage, never for normal use.

$ErrorActionPreference = 'Stop'
$HookPrefix = '[kuckuck-hook]'

function Write-HookWarning {
    param([string]$Message)
    # stderr: on exit 0 it surfaces in the terminal (user-visible), on
    # exit 2 Claude Code pipes it back to the model.
    [Console]::Error.WriteLine("$HookPrefix $Message")
}

function Invoke-FailClosedOrOpen {
    if ($env:KUCKUCK_HOOK_FAIL_OPEN -eq '1') {
        Write-HookWarning 'KUCKUCK_HOOK_FAIL_OPEN=1: continuing without pseudonymization (UNSAFE).'
        exit 0
    }
    exit 2
}

# Pre-flight: kuckuck on PATH?
$kuckuckCmd = Get-Command kuckuck -ErrorAction SilentlyContinue
if (-not $kuckuckCmd) {
    Write-HookWarning "kuckuck not found in PATH. Install via 'pip install kuckuck[cli]' or set KUCKUCK_HOOK_FAIL_OPEN=1 to bypass."
    Invoke-FailClosedOrOpen
}

# Read the whole stdin payload. -Raw keeps newlines intact so
# ConvertFrom-Json gets a single JSON document.
$rawInput = [Console]::In.ReadToEnd()
try {
    $payload = $rawInput | ConvertFrom-Json
} catch {
    Write-HookWarning "failed to parse stdin as JSON: $($_.Exception.Message)"
    Invoke-FailClosedOrOpen
}

$tool = if ($payload.tool_name) { $payload.tool_name } else { 'the tool' }

# Read and Edit pass the target as .tool_input.file_path; Grep uses
# .tool_input.path. Fall through to $null when neither is set so the
# hook stays a no-op for future tool shapes we do not know yet.
$file = $null
if ($payload.tool_input) {
    if ($payload.tool_input.file_path) {
        $file = $payload.tool_input.file_path
    } elseif ($payload.tool_input.path) {
        $file = $payload.tool_input.path
    }
}

if (-not $file) {
    exit 0
}

if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
    # Grep may target a directory; kuckuck only handles individual
    # files. Non-existent paths are Claude Code's problem, not ours.
    exit 0
}

# kuckuck is idempotent: already-pseudonymized tokens survive a second
# pass unchanged, so we can run it on every matching tool call without
# tracking state. Stdout is merged into stderr to keep the progress line
# out of the hookspecific stdout channel (which Claude Code may try to
# parse as JSON).
& $kuckuckCmd.Source run $file *>&2
$rc = $LASTEXITCODE

if ($rc -eq 0) {
    exit 0
}

if ($env:KUCKUCK_HOOK_FAIL_OPEN -eq '1') {
    Write-HookWarning "kuckuck failed on $file (exit $rc). KUCKUCK_HOOK_FAIL_OPEN=1: continuing without pseudonymization (UNSAFE)."
    exit 0
}

$message = @"
$HookPrefix Refusing to $tool $file directly (kuckuck exit $rc).
Run kuckuck_pseudonymize(file_path='$file') via the kuckuck-mcp server first,
then retry the $tool tool call on the now-pseudonymized file.
If the MCP server is unavailable, run 'kuckuck run $file' locally, or set
KUCKUCK_HOOK_FAIL_OPEN=1 in the environment to bypass this hook (UNSAFE).
"@
[Console]::Error.WriteLine($message)
exit 2
