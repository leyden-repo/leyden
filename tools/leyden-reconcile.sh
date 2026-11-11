#!/usr/bin/env bash
# leyden reconciliation backstop.
# Catches the failure mode the capture pipeline structurally can't see on its own: an entity that
# was TOUCHED in a recent session (via prose OR tool calls: an email sent, a file written, a thread
# fetched) but has NO recent log entry, e.g. an email drafted entirely through tool calls: the
# work lives in tool payloads and nothing lands in that entity's folder.
#
# Read-only: it never writes to the KB. It appends a report to capture.log and prints to stdout, so a
# human (or the next session) can decide whether to backfill.
#
# Usage: leyden-reconcile.sh [WINDOW_DAYS]   (default 7)
#        leyden-reconcile.sh --weekly        (no-op unless a week has passed; used by the load hook)
#
# Scheduling: this used to be documented as a weekly crontab entry (`0 8 * * 1 ...`) that was never
# actually installed, so the backstop the capture worker's ABANDON path relies on was not running at
# all. It is now invoked with --weekly from leyden-load.sh and self-throttles, matching the
# "rides on actual usage" model of the push job. No crontab entry needed.
set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kb.sh"; [ "$LDN_KB_OK" = 1 ] || exit 0   # KB, LDN_TOOLS, LDN_STATE (tools/_kb.sh)
LOG="$LDN_STATE/capture.log"
# shellcheck source=/dev/null
. "$LDN_TOOLS/_guard.sh"             # ldn_entity_pattern (word-bounded, shared with capture),
                                    # LDN_* taxonomy exports, LEYDEN_PY
PY="${LEYDEN_PY:-${LEYDEN_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}}"
EXTRACT="$LDN_TOOLS/_transcript.py"
STAMP="$LDN_STATE/.last-reconcile"
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Transcript directories to scan. Claude Code stores each session under ~/.claude/projects/<cwd
# with every character outside [a-zA-Z0-9] replaced by '-'>/ (names over 200 characters are cut
# at 200 and get a hash suffix), keyed on the directory Claude was LAUNCHED from. Which one that
# is depends on the install: the KB itself when it is a standalone repo, or the workspace that
# CONTAINS the KB when it sits among other repos. Derived, not hardcoded: a hardcoded path on any
# other machine made this backstop silently scan NOTHING and report "no gap", a permanent false
# negative in the net the capture ABANDON path relies on. It used to map only '/' and '.', so any
# workspace path with a space, underscore or other symbol was never found either.
# Every candidate that exists is scanned: the KB, its parent, and any extra directories listed
# under `capture.workspace_dirs` in leyden.yaml. A candidate whose encoded directory is not found
# falls back to scanning ~/.claude/projects/*/ for a transcript whose `cwd` is that directory.
# LEYDEN_PROJDIR overrides the whole list. `--projdirs` prints the list (leyden-metrics.sh uses it).
find_projdirs() {
  "$PY" - "$KB" "$(cd "$KB/.." && pwd)" <<'PY' 2>/dev/null || true
import glob, json, os, re, sys
home = os.path.join(os.path.expanduser("~"), ".claude", "projects")
cands = list(sys.argv[1:])
try:
    sys.path.insert(0, os.environ["LDN_TOOLS"])
    from _config import get
    extra = get("capture.workspace_dirs", [])
    cands += [extra] if isinstance(extra, str) else [str(x) for x in (extra or [])]
except Exception:
    pass
def encode(p):
    return re.sub(r"[^a-zA-Z0-9]", "-", p)
out, missing = [], []
for w in (c.strip().strip("\"'") for c in cands):
    if not w:
        continue
    found = False
    for path in dict.fromkeys((os.path.abspath(w), os.path.realpath(w))):
        e = encode(path)
        hits = [os.path.join(home, e)] if len(e) <= 200 else \
            glob.glob(os.path.join(home, glob.escape(e[:200]) + "-*"))
        for d in hits:
            if os.path.isdir(d):
                out.append(d); found = True
    if not found:
        missing.append(w)
if missing and os.path.isdir(home):
    want = set()
    for w in missing:
        want |= {os.path.abspath(w), os.path.realpath(w)}
    for d in sorted(glob.glob(os.path.join(home, "*"))):
        files = sorted(glob.glob(os.path.join(d, "*.jsonl")), key=os.path.getmtime, reverse=True)[:1]
        for f in files:
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for _, line in zip(range(50), fh):
                        cwd = json.loads(line).get("cwd") if '"cwd"' in line else None
                        if cwd:
                            if cwd in want:
                                out.append(d)
                            break
            except Exception:
                pass
for d in dict.fromkeys(out):
    print(d)
PY
}
PROJDIRS=()
if [ -n "${LEYDEN_PROJDIR:-}" ]; then
  PROJDIRS=("$LEYDEN_PROJDIR")
else
  while IFS= read -r d; do
    [ -n "$d" ] && PROJDIRS+=("$d")
  done < <(find_projdirs)
fi
if [ "${1:-}" = "--projdirs" ]; then
  [ "${#PROJDIRS[@]}" -gt 0 ] && printf '%s\n' "${PROJDIRS[@]}"
  exit 0
fi
# A new install has no transcripts yet: that is not an error, and the weekly run from the load
# hook must not fail on it. Nothing is stamped, so the next session simply looks again.
if [ "${#PROJDIRS[@]}" -eq 0 ]; then
  echo "reconcile: no Claude Code transcripts found for $KB or its parent under $HOME/.claude/projects yet (new install?), nothing to reconcile. Set LEYDEN_PROJDIR or capture.workspace_dirs if they live elsewhere."
  exit 0
fi

WINDOW=7
if [ "${1:-}" = "--weekly" ]; then
  if [ -f "$STAMP" ]; then
    last="$(cat "$STAMP" 2>/dev/null || echo 0)"
    [ $(( $(date +%s) - last )) -lt 604800 ] && exit 0
  fi
  date +%s > "$STAMP"
elif [ -n "${1:-}" ]; then
  WINDOW="$1"
fi

ENTITIES="$(ldn_entity_pattern "$KB")"
[ "$ENTITIES" = "__none__" ] && { echo "$(now) reconcile: no $LDN_ENTITIES found under $LDN_ENTITIES/" | tee -a "$LOG"; exit 0; }

# 1. Entities TOUCHED in transcripts modified within the window.
#
# The extraction is _transcript.py, the SAME parser leyden-capture.sh uses. It used to be a
# hand-copied inline duplicate that never received capture's KB-exclusion fix, so every session that
# read open.md (which names every open entity) or ran the lint counted as touching all of them.
# Result: this backstop reported 9 of 11 entities as "touched but not logged" on a normal run, i.e.
# it was pure noise and nobody could act on it.
#
# Three filters, all necessary, each one found by a false positive this script actually produced:
#   - KB content is stripped from tool payloads (see _transcript.py).
#   - Capture-worker transcripts are skipped entirely. They are Claude Code sessions too, so they
#     sit in the same projects/ directory, and their prose enumerates the hint list verbatim
#     ("Checked all other <entities> named in the hint list (a, b, ...)"). 17 of the 43 transcripts
#     in a 3-day window were capture runs feeding their own output back in.
#   - A block naming 4+ distinct entities is treated as an enumeration, not as work.
#
# STRONG vs WEAK. A gap is only reported for entities found in TOOL content: an email actually sent,
# a file actually written, a thread actually fetched. That is precisely the evidence class this
# backstop was built for (work that lives only in tool payloads). A name appearing
# just in conversation prose is reported separately as a passing mention and never as a gap, because
# "we discussed X" is not work that went unlogged, and treating it as one is what buried the real
# signal.
ENUM_ARGS=(--kb "$KB" --entities "$ENTITIES" --enum-threshold 4)

# Minimum mentions in the window before an entity counts as real evidence. One passing reference in
# a tool payload is not unlogged work.
# precedence: env > leyden.yaml retrieval.min_evidence > 5
MIN_EVIDENCE="${LEYDEN_MIN_EVIDENCE:-$("$PY" "$LDN_TOOLS/_config.py" get retrieval.min_evidence 5 2>/dev/null || echo 5)}"

# Emits "<count> <slug>" per entity for the given stream, across every non-capture transcript in
# the window. tr ' ' '-' normalises the "acme corp" spelling the match pattern allows.
evidence_counts() {   # <mode: prose|tools>
  local mode="$1" T
  while IFS= read -r T; do
    [ -f "$T" ] || continue
    "$PY" "$EXTRACT" is-capture-worker "$T" >/dev/null 2>&1 && continue
    "$PY" "$EXTRACT" "$mode" "$T" "${ENUM_ARGS[@]}" 2>/dev/null \
      | grep -ioE "$ENTITIES" || true
  done < <(find "${PROJDIRS[@]}" -name '*.jsonl' -mtime "-${WINDOW}" 2>/dev/null) \
    | tr 'A-Z' 'a-z' | tr ' ' '-' | grep -vE '^[[:space:]]*$' | ldn_canonicalise | sort | uniq -c \
    | awk '{print $1" "$2}' || true
}

# Slugs from an "<count> <slug>" stream whose count reaches the threshold.
above_threshold() { awk -v m="$MIN_EVIDENCE" '$1 >= m {print $2}' | sort -u || true; }

# 2. Entities whose log.md changed within the window (git if available, else file mtime).
logged_recently() {
  local slug f cutoff last
  cutoff="$(date -u -v-"${WINDOW}"d +%Y-%m-%d 2>/dev/null || date -u -d "-${WINDOW} days" +%Y-%m-%d)"
  for d in "$KB/$LDN_ENTITIES"/*/; do
    [ -d "$d" ] || continue
    slug="$(basename "$d")"
    f="$d/log.md"
    [ -f "$f" ] || continue
    last=""
    if git -C "$KB" rev-parse --git-dir >/dev/null 2>&1; then
      last="$(git -C "$KB" log -1 --format=%cd --date=short -- "$LDN_ENTITIES/$slug/log.md" 2>/dev/null || true)"
    fi
    if [ -n "$last" ]; then
      # Plain string compare is valid for ISO dates. Written as an explicit if rather than
      # `[ a ] || [ b ] && echo` because that idiom leaves a non-zero status as the loop body's last
      # command, which under `set -e` aborted the whole function on the final entity.
      if [ "$last" = "$cutoff" ] || [ "$last" \> "$cutoff" ]; then
        echo "$slug"
      fi
    elif [ -n "$(find "$f" -mtime "-${WINDOW}" 2>/dev/null)" ]; then
      echo "$slug"
    fi
  done | sort -u
}

# `|| true` on both: with `pipefail`, a `grep -v` that matches nothing returns 1 and the assignment
# fails, so `set -e` killed the script before it printed anything. That is what made
# `leyden-reconcile.sh 0` (and any genuinely quiet window) exit silently with no report at all:
# the happy path was the one that aborted.
clean() { grep -vE '^[[:space:]]*$' || true; }

TOOL_COUNTS="$(evidence_counts tools)"
PROSE_COUNTS="$(evidence_counts prose)"
STRONG="$(printf '%s\n' "$TOOL_COUNTS" | clean | above_threshold)"
WEAK="$(printf '%s\n' "$PROSE_COUNTS" | clean | above_threshold)"
LOGGED="$(logged_recently | clean | sort -u || true)"
GAP="$(comm -23 <(printf '%s\n' "$STRONG" | clean) <(printf '%s\n' "$LOGGED" | clean) || true)"
# Prose-only mentions with no log entry: informational, never a gap.
MENTIONED="$(comm -23 <(printf '%s\n' "$WEAK" | clean) \
                      <(printf '%s\n' "$STRONG" "$LOGGED" | clean | sort -u) || true)"
# The counts behind the verdict, so a gap can be judged without re-running anything.
detail() { printf '%s\n' "$1" | clean | awk '{printf "%s(%s) ", $2, $1}'; }

{
  echo "$(now) reconcile (window ${WINDOW}d, min evidence ${MIN_EVIDENCE} mentions, scanning ${#PROJDIRS[@]} transcript dir(s)):"
  echo "  tool evidence: $(detail "$TOOL_COUNTS")"
  echo "  conversation:  $(detail "$PROSE_COUNTS")"
  echo "  logged:        $(printf '%s\n' "$LOGGED" | tr '\n' ' ')"
  if [ -n "$GAP" ]; then
    echo "  GAP - real artifacts produced but NOT logged in window:"
    printf '%s\n' "$GAP" | sed 's/^/    - /'
  else
    echo "  no gap - every $LDN_ENTITY with tool-level evidence has a recent log entry"
  fi
  if [ -n "$MENTIONED" ]; then
    echo "  (discussed but no artifact, not a gap): $(printf '%s\n' "$MENTIONED" | tr '\n' ' ')"
  fi
} | tee -a "$LOG"

[ -n "$GAP" ] && exit 3 || exit 0
