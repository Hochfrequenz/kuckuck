#!/usr/bin/env bash
# Claude Code PreToolUse hook: auto-pseudonymize .eml/.msg files before
# Read / Edit / Grep. Installed via 'kuckuck install-claude-hook' or by
# copying this script to .claude/hooks/kuckuck-pseudo.sh and wiring it up
# manually in .claude/settings.json (see settings.example.json next door).
#
# Contract (see https://code.claude.com/docs/en/hooks):
#   - stdin:  Claude Code writes a PreToolUse JSON payload on stdin;
#             .tool_name holds the tool (Read / Edit / Grep),
#             .tool_input.file_path (Read, Edit) or .tool_input.path (Grep)
#             holds the target path.
#   - exit 0: tool call proceeds.
#   - exit 2: tool call is blocked; stderr is fed back to the model.
#
# Environment:
#   KUCKUCK_HOOK_FAIL_OPEN=1
#     Debug-only escape hatch. Missing kuckuck, missing jq, or a failing
#     pseudonymization will NOT block the tool call. UNSAFE - documented
#     so it is easy to switch on for local triage, never for normal use.

set -uo pipefail

HOOK_PREFIX="[kuckuck-hook]"

warn() {
    # Send a line to stderr. On exit 0, Claude Code shows stderr in the
    # terminal (user-visible, not model-visible). On exit 2, stderr is
    # piped back to the model as the tool-call error message.
    printf '%s %s\n' "$HOOK_PREFIX" "$1" >&2
}

fail_closed_or_open() {
    # Called from the pre-flight checks when a dependency is missing.
    # Honours KUCKUCK_HOOK_FAIL_OPEN so a broken local install does not
    # wedge the user's whole session; default is fail-closed.
    if [ "${KUCKUCK_HOOK_FAIL_OPEN:-0}" = "1" ]; then
        warn "KUCKUCK_HOOK_FAIL_OPEN=1: continuing without pseudonymization (UNSAFE)."
        exit 0
    fi
    exit 2
}

if ! command -v kuckuck >/dev/null 2>&1; then
    warn "kuckuck not found in PATH. Install via 'pip install kuckuck[cli]' or set KUCKUCK_HOOK_FAIL_OPEN=1 to bypass."
    fail_closed_or_open
fi

if ! command -v jq >/dev/null 2>&1; then
    warn "jq not found in PATH. Install via your package manager (e.g. 'apt install jq', 'brew install jq') or set KUCKUCK_HOOK_FAIL_OPEN=1 to bypass."
    fail_closed_or_open
fi

INPUT=$(cat)

# Parse the payload once up front. If jq cannot parse it we are past the
# point where a silent no-op is acceptable - otherwise a broken client
# sending garbage would fail-open by virtue of every later selector
# returning empty.
if ! printf '%s' "$INPUT" | jq empty >/dev/null 2>&1; then
    warn "failed to parse stdin as JSON (garbled payload or unexpected client)."
    fail_closed_or_open
fi

TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // "the tool"')
# Read and Edit pass the target as .tool_input.file_path; Grep uses
# .tool_input.path. Fall through to empty when neither is set (e.g. a
# future tool whose payload shape we do not know yet) so the hook stays
# a no-op instead of false-blocking.
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_input.path // empty')

if [ -z "$FILE" ]; then
    exit 0
fi

if [ ! -f "$FILE" ]; then
    # Grep may target a directory; kuckuck only handles individual files.
    # Non-existent paths are Claude Code's problem, not ours.
    exit 0
fi

# kuckuck is idempotent: already-pseudonymized tokens survive a second
# pass unchanged, so we can run it on every matching tool call without
# tracking state. Stdout is redirected to stderr to keep the progress
# line out of the hook-specific stdout channel (which Claude Code may
# try to parse as JSON).
#
# We capture kuckuck's exit status via $? on the very next line instead
# of via an 'if kuckuck ...; then exit 0; fi' wrapper, because $? after
# 'fi' reflects fi's own 0, not the command's exit code.
kuckuck run "$FILE" 1>&2
rc=$?

if [ "$rc" -eq 0 ]; then
    exit 0
fi

if [ "${KUCKUCK_HOOK_FAIL_OPEN:-0}" = "1" ]; then
    warn "kuckuck failed on $FILE (exit $rc). KUCKUCK_HOOK_FAIL_OPEN=1: continuing without pseudonymization (UNSAFE)."
    exit 0
fi

cat >&2 <<EOF
$HOOK_PREFIX Refusing to $TOOL $FILE directly (kuckuck exit $rc).
Run kuckuck_pseudonymize(file_path='$FILE') via the kuckuck-mcp server first,
then retry the $TOOL tool call on the now-pseudonymized file.
If the MCP server is unavailable, run 'kuckuck run $FILE' locally, or set
KUCKUCK_HOOK_FAIL_OPEN=1 in the environment to bypass this hook (UNSAFE).
EOF
exit 2
