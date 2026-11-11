#!/usr/bin/env bash
# leyden batched auto-push: regenerate the dashboards, snapshot workspace config, commit, and push,
# at most once per STALE_INTERVAL (default 20h, so the first Claude session of the day does it).
# Called (backgrounded) from the SessionStart load hook, so this rides on actual usage.
#
#   leyden-push-if-stale.sh            stale-gated (the hook path)
#   leyden-push-if-stale.sh --force    push now regardless of the interval (`bin/leyden push`);
#                                      progress is echoed to stdout as well as capture.log
set -euo pipefail
[ -n "${LEYDEN_CAPTURE:-}" ] && exit 0

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force|-f) FORCE=1 ;;
    -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "leyden-push-if-stale.sh: unknown argument '$arg'" >&2; exit 2 ;;
  esac
done

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kb.sh"; [ "$LDN_KB_OK" = 1 ] || exit 0   # KB, LDN_TOOLS, LDN_STATE (tools/_kb.sh)
LOG="$LDN_STATE/capture.log"
STAMP="$LDN_STATE/.last-push"
# Pin the interpreter (LEYDEN_PYTHON, else python3 on PATH, else the system one), same knob as
# every other tool. A different sqlite build writing index.db, or a missing python, made all six
# steps below silently no-op behind `|| true` for a full cycle.
PY="${LEYDEN_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"
# precedence: env > leyden.yaml push.interval_seconds > 72000 (20h: first session of the day pushes)
INTERVAL="${LEYDEN_PUSH_INTERVAL:-$("$PY" "$LDN_TOOLS/_config.py" get push.interval_seconds 72000 2>/dev/null || echo 72000)}"
now_s="$(date +%s)"
# shellcheck source=/dev/null
. "$LDN_TOOLS/_guard.sh"

# say <msg>: always to the log; also to stdout when a human asked for this run (--force).
say() {
  echo "$(date -u +%FT%TZ) $*" >> "$LOG"
  [ "$FORCE" -eq 1 ] && echo "$*" || true
}

# A non-numeric interval ("20h") used to reach `[ -lt ]`, which errored, so the gate never applied
# and the full cycle ran on every session start. Say so and keep the default.
case "$INTERVAL" in
  ''|*[!0-9]*) say "push.interval_seconds must be a whole number of seconds (got '$INTERVAL'); using 72000"; INTERVAL=72000 ;;
esac

# No remote: the dashboards, index and the local commit still run (gated like any cycle); only the
# push itself is skipped. It used to exit here, so a local-only KB never got a refreshed dashboard.
HAVE_REMOTE=1
git -C "$KB" remote get-url origin >/dev/null 2>&1 || HAVE_REMOTE=0

# Never publish the KB to the Leyden TOOL repo. `git clone <leyden> my-kb` leaves origin pointing
# at it, and this job's `git add -A` + push then sent entities, cases and leyden.yaml (with the
# engineer's email) there. Refused when origin is the tool repo (or equals leyden-upstream) or the
# branch tracks a remote other than origin. Fails CLOSED: if the check cannot run, nothing is
# pushed. `leyden init --remote <url>` fixes the setup.
origin_check() {
  local why
  if why="$("$PY" "$LDN_TOOLS/leyden-init.py" --check-origin 2>&1)"; then
    return 0
  fi
  say "push REFUSED: ${why:-origin check failed to run ($PY)}"
  return 1
}
if [ "$HAVE_REMOTE" -eq 1 ]; then
  origin_check || exit "$FORCE"   # a human-run `leyden push` gets a non-zero exit
fi

# ── Stale gate FIRST ──────────────────────────────────────────────────────────────────────────────
# Everything below is expensive: three python passes (issues, lint, graph; graph.html alone is
# ~750 KB regenerated from scratch), an rsync snapshot, and a git commit. All of it used to run on
# EVERY SessionStart, with only the final `git push` behind this gate. That is why graph.html,
# issues.html and closed.md were rewritten several times a day, and it put a `git add -A` commit in
# direct competition with the capture drain that the same hook had just backgrounded.
# Gate the whole job, not just the push. --force skips the gate, nothing else.
if [ "$FORCE" -eq 0 ] && [ -f "$STAMP" ]; then
  last="$(cat "$STAMP" 2>/dev/null || echo 0)"
  if [ $((now_s - last)) -lt "$INTERVAL" ]; then
    exit 0
  fi
fi

# Defer the WHOLE cycle while a capture drain is in flight, without the stamp, so it retries
# next session. The drain edits KB files over minutes without holding the git lock: the
# open.md sync's read-modify-write would lose its edits, and (worse) the `pull --rebase
# --autostash` below would stash the drain's half-written files and lose them in a stash entry
# nothing inspects, or stage them as deletions. Deferring one cycle is free. This holds for
# --force too: a forced push over a half-written drain is exactly the loss mode described.
#
# WAIT for the drain rather than skipping the cycle outright. Both jobs are launched from the same
# SessionStart hook and the drain claims its lock FIRST (leyden-load.sh calls it before
# backgrounding this one), so an immediate defer meant the push lost the race on essentially every
# session that had anything queued, which is almost all of them. Measured on a KB that ran this
# daily for four months: 43 deferrals against 39 pushes, and a job documented as "the first session
# of the day pushes" that actually landed once every two to six days. Waiting costs nothing here,
# this job is already detached and off the critical path; only a drain that outlasts the wait
# defers, and that one really is better retried next session.
DRAIN_WAIT="${LEYDEN_DRAIN_WAIT:-$("$PY" "$LDN_TOOLS/_config.py" get push.drain_wait_seconds 900 2>/dev/null || echo 900)}"
case "$DRAIN_WAIT" in ''|*[!0-9]*) DRAIN_WAIT=900 ;; esac
waited=0
# Reap before and during the wait: a drain that died leaves its lock, which used to defer every push.
ldn_lock_reap "$LDN_STATE/.capture.lock" 10
while [ -d "$LDN_STATE/.capture.lock" ] && [ "$waited" -lt "$DRAIN_WAIT" ]; do
  sleep 10
  waited=$((waited + 10))
  ldn_lock_reap "$LDN_STATE/.capture.lock" 10
done
if [ -d "$LDN_STATE/.capture.lock" ]; then
  say "push cycle deferred: capture drain still running after ${waited}s (retry next session)"
  # A human-run `leyden push` that did nothing must say so: non-zero exit + a session-start line.
  [ "$FORCE" -eq 1 ] && ldn_health_set push-deferred "Your last 'leyden push' did not run: a capture drain held its lock. Retry: leyden push"
  exit "$FORCE"
fi
if [ "$waited" -gt 0 ]; then
  say "waited ${waited}s for the capture drain to finish"
  # Re-check the stale gate after the wait: a second session started during those minutes may
  # have passed the gate and completed the whole cycle while we were blocked, and running it
  # twice back to back is pure duplicated work (three python passes and a 750 KB graph.html).
  if [ "$FORCE" -eq 0 ] && [ -f "$STAMP" ]; then
    last="$(cat "$STAMP" 2>/dev/null || echo 0)"
    if [ $(( $(date +%s) - last )) -lt "$INTERVAL" ]; then
      say "push cycle skipped: another session pushed while we waited for the drain"
      exit 0
    fi
  fi
fi

# A rebase/merge left half-done (a killed `pull --rebase`, or a hand rebase) must be finished by a
# person: everything below would commit onto a detached HEAD with conflict markers.
if ldn_repo_busy "$KB"; then
  say "push cycle skipped: KB has a rebase/merge in progress or a detached HEAD (run: git -C \"$KB\" status)"
  ldn_health_set repo-busy "Your KB has an unfinished git rebase or merge, so nothing is being committed or pushed. Run: git -C \"$KB\" status"
  exit "$FORCE"
fi

# Frecency aging (zoxide-style) BEFORE the dashboards and the index rebuild, so the committed
# frontmatter and the search index agree. A no-op until total rank exceeds LEYDEN_MAX_RANK.
"$PY" "$LDN_TOOLS/leyden-index.py" --age >>"$LOG" 2>&1 || true

# KB health: sync open.md's status column from case files (single source of truth; narrative
# cells untouched), regenerate the generated dashboards FIRST (so lint sees a current closed.md),
# then lint (report-only here; run tools/leyden-lint.py by hand to gate) + graph refresh.
"$PY" "$LDN_TOOLS/leyden-issues.py" --sync-open --fix >>"$LOG" 2>&1 || true
"$PY" "$LDN_TOOLS/leyden-issues.py" >>"$LOG" 2>&1 || true
"$PY" "$LDN_TOOLS/leyden-index.py" >>"$LOG" 2>&1 || true
{ echo "$(date -u +%FT%TZ) lint:"; "$PY" "$LDN_TOOLS/leyden-lint.py"; } >>"$LOG" 2>&1 || true
"$PY" "$LDN_TOOLS/leyden-graph.py" >>"$LOG" 2>&1 || true

# Refresh the workspace-config snapshot and commit it (+ any pending KB changes) behind the guard.
# The commit guard (ldn_commit_guard, from tools/_guard.sh) serialises on the shared git lock, so
# this cannot collide with a capture commit.
"$LDN_TOOLS/leyden-sync-workspace.sh" >>"$LOG" 2>&1 || true

# ── Commit + integrate + push under ONE git-op lock ──────────────────────────────────────────────
# Everything from here on (commit, ahead count, pull --rebase --autostash, push) holds the SAME lock
# as the capture commit (ldn_git_lock, .leyden/.git-op.lock). Only the commit used to take it, so a
# capture commit could land in the middle of the rebase. The commit guard runs with
# LDN_GIT_LOCK_HELD=1 so it uses this lock instead of taking its own. The drain check is repeated
# while holding the lock: the wait above was check-then-act, and a drain may have started since.
if ! declare -F ldn_git_lock >/dev/null 2>&1 || ! declare -F ldn_commit_guard >/dev/null 2>&1; then
  say "push skipped: ldn_git_lock/ldn_commit_guard not defined in tools/_guard.sh"
  exit 0
fi
if ! ldn_git_lock "$KB" 180; then
  say "push skipped: git lock busy (another leyden job holds it), retry next cycle"
  exit 0
fi
trap 'ldn_git_unlock "$KB"' EXIT
if [ -d "$LDN_STATE/.capture.lock" ]; then
  say "push cycle deferred: a capture drain started while the cycle ran (retry next session)"
  [ "$FORCE" -eq 1 ] && ldn_health_set push-deferred "Your last 'leyden push' did not run: a capture drain held its lock. Retry: leyden push"
  exit "$FORCE"
fi
ldn_health_clear push-deferred
crc=0
LDN_GIT_LOCK_HELD=1 ldn_commit_guard "$KB" "sync: workspace-config + kb snapshot $(date -u +%Y-%m-%d)" "$LOG" || crc=$?
if [ "$crc" -ge 3 ]; then
  # The commit did not happen (repo busy, git add or commit failed). Do NOT stamp: the old code went
  # on to "nothing to push", stamped, and the KB then sat uncommitted for a full interval.
  say "push cycle stopped: the KB commit failed (rc=$crc); will retry next session"
  exit "$FORCE"
fi
if [ "$HAVE_REMOTE" -eq 0 ]; then
  echo "$now_s" > "$STAMP"
  say "dashboards refreshed and committed; push skipped: no 'origin' remote configured"
  exit 0
fi
origin_check || exit "$FORCE"   # a human-run `leyden push` gets a non-zero exit

# Any unpushed commits? (treat "no upstream" as needs-push)
if git -C "$KB" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  ahead="$(git -C "$KB" rev-list --count '@{u}..HEAD' 2>/dev/null || echo 1)"
else
  ahead=1
fi
if [ "${ahead:-0}" -eq 0 ]; then
  # Nothing to push, but the expensive regeneration DID run. Stamp it so we don't redo all of that
  # on the next session start just because there was no commit to push.
  echo "$now_s" > "$STAMP"
  ldn_health_clear push-blocked
  say "nothing to push (branch is up to date with its upstream)"
  exit 0
fi

# git's own last stderr line, for the log: a bare "FAILED (conflict?)" sent people hunting for a
# conflict when the real cause was e.g. "no such ref was fetched" (the upstream branch is gone).
# All of git's non-hint lines joined into one (its messages wrap), capped for the log line.
git_reason() { printf '%s\n' "$1" | grep -vE '^[[:space:]]*$|^hint:' | tr '\n' ' ' | cut -c1-300; }

# Integrate the remote FIRST (two-machine / team safety): rebase our unpushed commits onto
# whatever landed on origin. --autostash covers the working tree's uncommitted edits. On a real
# conflict, abort cleanly and skip this push cycle: never leave a half-rebased repo for the
# hooks to trip over; the next cycle retries and the conflict surfaces in the log.
# Never wait on a prompt: this runs in the background, and a hung pull/push held the git lock so
# every capture skipped its commit (found in testing). A user's own core.sshCommand is kept.
export GIT_TERMINAL_PROMPT=0
if [ -z "${GIT_SSH_COMMAND:-}" ] && [ -z "$(git -C "$KB" config core.sshCommand 2>/dev/null)" ]; then
  export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=20"
fi
if git -C "$KB" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  if ! err="$(git -C "$KB" pull --rebase --autostash -q 2>&1)"; then
    printf '%s\n' "$err" >> "$LOG"
    git -C "$KB" rebase --abort >/dev/null 2>&1 || true
    say "pull --rebase FAILED: $(git_reason "$err"); push skipped, will retry next cycle"
    if printf '%s' "$err" | grep -qiE 'could not resolve|unable to access|could not read from remote|permission denied|authentication failed|repository not found|timed out|connection (refused|reset|closed)|terminal prompts disabled|host key'; then
      ldn_health_set push-blocked "Your KB has not been pushed: its remote cannot be reached ($(git_reason "$err" | cut -c1-120)). Check the network and your git access, then: leyden push"
    else
      ldn_health_set push-blocked "Your KB has not been pushed: pulling from its remote conflicts ($(git_reason "$err" | cut -c1-120)). Resolve with: git -C \"$KB\" pull --rebase"
    fi
    exit "$FORCE"
  fi
fi

# Outgoing-commit scan. The commit guard redacts what LEYDEN commits, but a commit made by hand (or
# by another tool) in the KB went out unscanned. Every added line in every commit about to leave is
# run through the same scanner; any hit stops the push (nothing is rewritten: a person decides).
if git -C "$KB" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  range=('@{u}..HEAD')
else
  range=(HEAD --not --remotes)
fi
if ! git -C "$KB" -c core.quotePath=false log -p --text --no-color --format= "${range[@]}" 2>>"$LOG" \
     | LC_ALL=C tr -d '\000' | { LC_ALL=C grep -aE '^\+' || true; } \
     | "$PY" "$LDN_TOOLS/leyden-redact.py" --scan - >>"$LOG" 2>&1; then
  say "push REFUSED: an outgoing commit contains something that looks like a secret (see the scan lines above in capture.log). Fix the commit (git -C \"$KB\" log -p @{u}..HEAD), then run: leyden push"
  ldn_health_set push-secret "Your KB push is blocked: an unpushed commit looks like it contains a secret. See $LOG"
  exit "$FORCE"
fi
ldn_health_clear push-secret

# First push of a branch has no upstream yet: set it, so the ahead-count works from then on.
if git -C "$KB" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  push_args=(push -q)
else
  push_args=(push -q -u origin HEAD)
fi
if err="$(git -C "$KB" "${push_args[@]}" 2>&1)"; then
  echo "$now_s" > "$STAMP"
  say "pushed ($ahead commits)"
  ldn_health_clear push-blocked
else
  printf '%s\n' "$err" >> "$LOG"
  say "push failed: $(git_reason "$err")"
  ldn_health_set push-blocked "Your KB push failed: $(git_reason "$err" | cut -c1-140)"
  [ "$FORCE" -eq 1 ] && exit 1
fi
exit 0
