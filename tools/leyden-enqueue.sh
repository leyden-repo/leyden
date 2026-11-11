#!/usr/bin/env bash
# leyden SessionEnd hook: enqueue the ended session's transcript for capture.
# SessionEnd can't reliably run the heavy headless capture (the session is terminating and
# Claude Code may kill child processes), so we only APPEND the transcript path to a queue.
# The next SessionStart (leyden-load.sh) drains the queue and runs the capture in the
# background, which is safe there because that session stays alive. Instant + non-blocking.
# `leyden charge` (leyden-drain.sh --now) drains the same queue on demand.
set -euo pipefail

# Recursion guard: the headless `claude` spawned by the capture inherits this env; its own
# SessionEnd must NOT enqueue its transcript (would loop / capture the capturer).
[ -n "${LEYDEN_CAPTURE:-}" ] && exit 0

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kb.sh"; [ "$LDN_KB_OK" = 1 ] || exit 0   # KB, LDN_TOOLS, LDN_STATE (tools/_kb.sh)
PY="${LEYDEN_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"
INPUT="$(cat)"
# The path is queued in its CANONICAL form (symlinks resolved, absolute): offsets are keyed on that
# spelling, and /tmp vs /private/tmp or a relative path would otherwise be a second, re-paid capture.
T="$(printf '%s' "$INPUT" | "$PY" -I -B -c 'import sys,json,os
p=json.load(sys.stdin).get("transcript_path") or ""
print(os.path.realpath(p) if p else "")' 2>/dev/null || true)"
{ [ -n "$T" ] && [ -f "$T" ]; } && printf '%s\n' "$T" >> "$LDN_STATE/capture-queue.txt"
exit 0
