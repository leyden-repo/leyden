#!/usr/bin/env bash
# leyden capture-queue drain: run leyden-capture.sh over every transcript queued by the SessionEnd
# hook (leyden-enqueue.sh), under a single-drain lock so the offset store keeps one writer.
#
#   leyden-drain.sh --background        claim the queue and drain it DETACHED (SessionStart hook)
#   leyden-drain.sh --now               claim the queue and drain it in the FOREGROUND, printing
#                                       what each capture did (`leyden charge` with no argument)
#   leyden-drain.sh --one <transcript>  capture ONE transcript under the same lock (`leyden charge
#                                       <transcript>`); if a drain is running, queue it for the next
#   leyden-drain.sh <work-file> <lock>  internal: the detached worker spawned by --background
#
# Every capture runs under this lock, including a single-transcript `leyden charge <path>`: that used
# to exec the worker directly, and offset_commit is a read-modify-mv, so 16 parallel charges lost 3
# offsets (those transcripts would be captured, and paid for, again).
#
# Claiming is atomic: `mkdir` takes the lock, `mv capture-queue.txt -> .processing` grabs every
# path enqueued so far, and anything enqueued after the mv lands in a fresh queue for the next
# drain. Nothing is ever lost between the two.
#
# The worker is its own PROCESS (not an inline subshell in the hook) for exactly one reason: the
# lock's liveness check needs the drain's OWN pid, and on macOS bash 3.2 there is no $BASHPID and
# no portable way to get a subshell's pid ($(sh -c 'echo $PPID') records the command-substitution
# fork, already dead: it made every SessionStart reap a LIVE drain's lock and
# start a second concurrent drain, breaking the offset store's single-writer invariant). In a real
# script, plain $$ IS the drain's pid. That's the whole trick.
set -uo pipefail
[ -n "${LEYDEN_CAPTURE:-}" ] && exit 0   # the headless capture child must never drain

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kb.sh"; [ "$LDN_KB_OK" = 1 ] || exit 0   # KB, LDN_TOOLS, LDN_STATE (tools/_kb.sh)
QUEUE="$LDN_STATE/capture-queue.txt"
LOCK="$LDN_STATE/.capture.lock"
WORK="$QUEUE.processing"
LOG="$LDN_STATE/capture.log"
PY="${LEYDEN_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"
export LEYDEN_PYTHON="$PY"
DRY="${LEYDEN_DRY_RUN:-}"
# shellcheck source=/dev/null
. "$LDN_TOOLS/_offsets.sh"   # ldn_realpath, offset_get, retry_get, ldn_requeue
# _kb.sh provides ldn_lock_reap / ldn_pid_alive / ldn_mtime.

# ── Lock hygiene ──────────────────────────────────────────────────────────────────────────────────
# Liveness is decided by the OWNER PIDS ($LOCK/pid: the drain; $LOCK/worker: the capture worker and
# its headless child), not by the lock's age: a drain legitimately runs for many minutes. Reaping
# goes through ldn_lock_reap (tools/_kb.sh): an atomic rename, so concurrent reapers cannot delete a
# lock a third process has just taken, and a pid reused by an unrelated newer process does not keep
# a dead lock alive forever.
#
# Every claim has its OWN work file ($QUEUE.processing.<claimer pid>). With one shared
# ".processing" name, a drain that had been judged dead (stale lock after a reboot) could have its
# claim folded back while it was still running, and its `rm -f $WORK` then deleted ANOTHER drain's
# claim: 4 concurrent drains lost a transcript 2 runs in 10 and captured one twice 5 runs in 10.
reap_stale_lock() { ldn_lock_reap "$LOCK" 10; }

# Claims left by a drain that died. Only called while THIS process holds the lock, so no live drain
# can own any of them: fold them back into the queue for retry. The pre-split single-name claim
# file is included for upgrades.
recover_orphaned_claims() {
  local f
  for f in "$QUEUE".processing "$QUEUE".processing.*; do
    [ -f "$f" ] || continue
    [ "$f" = "$WORK" ] && continue
    cat "$f" >> "$QUEUE" 2>/dev/null && rm -f "$f"
  done
}

have_claims() {
  local f
  for f in "$QUEUE".processing "$QUEUE".processing.*; do [ -f "$f" ] && return 0; done
  return 1
}

# claim_queue -> 0 if this process now holds the lock and $WORK, 1 if there is nothing to drain,
# 2 if another drain holds the lock. The pid is written IMMEDIATELY after mkdir (the claimer is alive
# for the whole claim), so a reaper never sees a fresh lock with no owner for longer than one write.
claim_queue() {
  reap_stale_lock   # first: a dead lock with an empty queue otherwise lives on and blocks every push
  [ -s "$QUEUE" ] || have_claims || return 1
  mkdir "$LOCK" 2>/dev/null || return 2
  echo "$$" > "$LOCK/pid" 2>/dev/null || true
  WORK="$QUEUE.processing.$$"
  recover_orphaned_claims
  if [ -s "$QUEUE" ] && mv "$QUEUE" "$WORK" 2>/dev/null; then
    return 0
  fi
  release_lock
  return 1
}

# release_lock: remove the lock only while it is still OURS. A drain whose lock was (wrongly) reaped
# must not delete the lock a later drain took.
release_lock() {
  local p
  p="$(head -n 1 "$LOCK/pid" 2>/dev/null || true)"
  if [ -z "$p" ] || [ "$p" = "$$" ] || [ "$p" = "${LDN_CLAIMER:-}" ]; then
    rm -rf "$LOCK" 2>/dev/null || true
  fi
}

# run_one <verbose> <transcript>: one capture, as a child whose pid we know, with a post-mortem.
# A worker that DIED (SIGKILL, OOM, an abort its own traps could not catch) neither advanced the
# offset nor re-queued itself; the claim file was then deleted and the transcript was never retried,
# with its render/prompt temp files left in tools/. That case is detected here from the outside:
# non-zero exit + offset unchanged + not re-queued -> re-queue (counted), and its temp files and any
# orphaned headless child are cleaned up.
run_one() {
  local verbose="$1" t="$2" off0 r0 pid rc=0 before=0 out="" wpid cpid
  off0="$(offset_get capture "$t")"; r0="$(retry_get "$t")"
  [ -f "$LOG" ] && before="$(wc -c < "$LOG" | tr -d ' ')"
  if [ -n "$verbose" ]; then
    # Foreground: the worker's stdout (only dry runs print anything there) goes to the caller,
    # its stderr to the log as usual, and the log lines it appended are relayed afterwards.
    # Outside the KB: anything new under it reads as "the worker wrote something" and is committed.
    out="$(mktemp "${TMPDIR:-/tmp}/leyden-capture-out.XXXXXX")"
    if [ -n "$DRY" ]; then
      # A dry run writes nothing to capture.log, its stderr included.
      LEYDEN_DRAIN_LOCK="$LOCK" LEYDEN_BG=1 LEYDEN_TRANSCRIPT="$t" "$LDN_TOOLS/leyden-capture.sh" \
        > "$out" &
    else
      LEYDEN_DRAIN_LOCK="$LOCK" LEYDEN_BG=1 LEYDEN_TRANSCRIPT="$t" "$LDN_TOOLS/leyden-capture.sh" \
        > "$out" 2>>"$LOG" &
    fi
  else
    LEYDEN_DRAIN_LOCK="$LOCK" LEYDEN_BG=1 LEYDEN_TRANSCRIPT="$t" "$LDN_TOOLS/leyden-capture.sh" \
      >>"$LOG" 2>&1 &
  fi
  pid=$!
  wait "$pid" || rc=$?
  if [ "$rc" -ne 0 ] && [ -z "$DRY" ]; then
    if [ -f "$LOCK/worker" ]; then
      read -r wpid cpid < "$LOCK/worker" || true
      [ -n "${cpid:-}" ] && kill "$cpid" 2>/dev/null
      rm -f "$LOCK/worker"
    fi
    rm -f "$LDN_STATE/.capture-render.$pid.txt" "$LDN_STATE/.capture-prompt.$pid.md" \
          "$LDN_STATE/.capture-out.$pid.json" "${TMPDIR:-/tmp}/leyden-capture-settings.$pid.json" \
          "$LDN_STATE/.capture-pre.$pid.tsv" "$LDN_STATE/.capture-post.$pid.tsv" "$LDN_STATE/.capture-only.$pid.lst" 2>/dev/null
    if [ "$(offset_get capture "$t")" = "$off0" ] && [ "$(retry_get "$t")" = "$r0" ] \
       && ! grep -qxF "$t" "$QUEUE" 2>/dev/null; then
      ldn_requeue "$t" "capture worker died (rc=$rc)" "$LOG" counted
    fi
  fi
  if [ -n "$verbose" ]; then
    [ -s "$out" ] && sed 's/^/    /' "$out"
    if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" | tr -d ' ')" -gt "$before" ]; then
      tail -c "+$((before + 1))" "$LOG" | sed 's/^/    /'
    elif [ ! -s "$out" ]; then
      # Silent worker + silent log = the offset dedup short-circuited (nothing new since the
      # last capture). Every other path writes at least one log line.
      echo "    (already captured up to its current size: nothing to do)"
    fi
    rm -f "$out"
  fi
  return 0
}

# drain_work [verbose]: process every claimed transcript, then release the claim and the lock.
# With `verbose`, print the capture.log lines each transcript produced so a foreground caller sees
# skip/start/requeue/commit verdicts instead of silence.
drain_work() {
  local verbose="${1:-}" t n=0 seen=""
  echo "$$" > "$LOCK/pid" 2>/dev/null || true
  while IFS= read -r t; do
    { [ -n "$t" ] && [ -f "$t" ]; } || continue
    # One capture per transcript per drain, whichever spelling queued it (relative, /tmp vs
    # /private/tmp): `sort -u` only dedups identical strings.
    t="$(ldn_realpath "$t")"
    case "
$seen
" in *"
$t
"*) continue ;; esac
    seen="$seen
$t"
    n=$((n + 1))
    [ -n "$verbose" ] && echo "[$n] $t"
    run_one "$verbose" "$t"
  done < <(sort -u "$WORK")
  if [ -n "$DRY" ]; then
    # A dry run inspects; it must not consume the queue it inspected.
    cat "$WORK" >> "$QUEUE" 2>/dev/null || true
  fi
  rm -f "$WORK"
  release_lock
  [ -n "$verbose" ] && echo "drained $n transcript(s)${DRY:+ (dry run: queue left as it was)}"
  return 0
}

case "${1:-}" in
  --now)
    claim_queue; rc=$?
    case "$rc" in
      0) drain_work verbose ;;
      1) echo "capture queue is empty: nothing to drain"; exit 0 ;;
      *) echo "a drain is already running (pid $(cat "$LOCK/pid" 2>/dev/null || echo '?')); try again later"; exit 1 ;;
    esac ;;
  --one)
    [ -n "${2:-}" ] || { echo "usage: leyden-drain.sh --one <transcript>" >&2; exit 2; }
    [ -f "$2" ] || { echo "leyden charge: no such transcript: $2" >&2; exit 2; }
    t="$(ldn_realpath "$2")"
    reap_stale_lock
    if mkdir "$LOCK" 2>/dev/null; then
      echo "$$" > "$LOCK/pid"
      trap 'release_lock' EXIT
      trap 'exit 143' TERM; trap 'exit 130' INT
      echo "[1] $t"
      run_one verbose "$t"
      exit 0
    fi
    holder="$(cat "$LOCK/pid" 2>/dev/null || echo '?')"
    if [ -n "$DRY" ]; then
      echo "a drain is already running (pid $holder): dry run not started; try again later"; exit 1
    fi
    printf '%s\n' "$t" >> "$QUEUE"
    echo "a drain is already running (pid $holder): queued $t for the next drain (\`leyden charge\` or the next session start)"
    exit 0 ;;
  --background)
    if claim_queue; then
      # Re-exec as a detached WORKER so the pid in $LOCK/pid is the worker's own (see the header).
      # The claimer records the worker's pid ITSELF before returning ($! is the nohup child, which
      # execs into this script): previously the lock held no pid from the claimer's exit until the
      # worker started, and a reaper treated that window as a dead lock.
      LDN_CLAIMER=$$ nohup "$0" "$WORK" "$LOCK" >/dev/null 2>&1 &
      echo "$!" > "$LOCK/pid" 2>/dev/null || true
      disown 2>/dev/null || true
    fi
    exit 0 ;;
  "")
    echo "usage: leyden-drain.sh --now | --background | --one <transcript> | <claimed-queue-file> <lock-dir>" >&2; exit 2 ;;
  *)
    # Internal worker form: the claim has already been made by --background; $1/$2 are its paths.
    WORK="$1"; LOCK="${2:?usage: leyden-drain.sh <claimed-queue-file> <lock-dir>}"
    drain_work ;;
esac
