# Locate the knowledge base (KB) this Leyden tool serves. `source` this; do not execute directly.
#
# The tool repo (this checkout) and the KB are separate git repos: the tool is pull-only and holds
# no data; the KB is your own private repo. Resolution order:
#   1. LEYDEN_HOME                 explicit override (tests, a second KB, scripts)
#   2. <tool root>/.kb-path        one line, written by `leyden init`, gitignored: relative to the
#                                  tool's parent (the workspace) or absolute (older inits)
#   3. legacy in-clone KB          the tool root itself carries leyden.yaml (pre-split layout)
# Always returns 0 (bash 3.2 under `set -e` exits on a non-zero sourced file even inside `||`).
# Callers test LDN_KB_OK: 1 = KB found, 0 = nothing configured. Hooks then `exit 0` silently (they
# must never block a Claude session); interactive verbs print "no KB configured, run: leyden init".
#
# Sets and exports:
#   KB / LEYDEN_HOME   absolute KB path (exported so every child, python included, agrees)
#   LDN_TOOLS          absolute tools/ dir of this checkout
#   LDN_TOOL_ROOT      absolute tool repo root
#   LDN_STATE          $KB/.leyden: ALL machine-local runtime state (logs, queue, locks, offsets,
#                      index.db, metrics). Gitignored in the KB. Never inside the tool repo,
#                      except in legacy mode where the KB IS the tool root.
#   LDN_KB_SOURCE      env | kb-path | legacy (doctor reports it)
# Also sets PYTHONDONTWRITEBYTECODE=1 so running a python tool never writes __pycache__ into the
# tool checkout.

# pwd -P: physical paths, as _config.py resolves them, so a relative .kb-path means the same thing
# to both when the tool checkout is reached through a symlink.
LDN_TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LDN_TOOL_ROOT="$(cd "$LDN_TOOLS/.." && pwd -P)"
export LDN_TOOLS LDN_TOOL_ROOT PYTHONDONTWRITEBYTECODE=1

ldn_resolve_kb() {
  local kb="" src=""
  if [ -n "${LEYDEN_HOME:-}" ]; then
    # A parent leyden process exports LEYDEN_HOME and LDN_KB_SOURCE together; keep its answer so
    # doctor reports where the KB really came from. A bare LEYDEN_HOME is the user's override.
    kb="$LEYDEN_HOME"; src="${LDN_KB_SOURCE:-env}"
  elif [ -f "$LDN_TOOL_ROOT/.kb-path" ]; then
    kb="$(head -n 1 "$LDN_TOOL_ROOT/.kb-path" 2>/dev/null | tr -d '\r')"; src=kb-path
    # `~` / `~/x` written by hand: _config.py expands it, so the shell side must too or the two
    # disagree ("no KB configured" here, a real KB there).
    case "$kb" in "~") kb="$HOME" ;; "~/"*) kb="$HOME/${kb#\~/}" ;; esac
    # Relative = relative to the workspace, so a moved or copied workspace keeps its own KB.
    case "$kb" in ""|/*) ;; *) kb="$(dirname "$LDN_TOOL_ROOT")/$kb" ;; esac
  elif [ -f "$LDN_TOOL_ROOT/leyden.yaml" ]; then
    kb="$LDN_TOOL_ROOT"; src=legacy
  fi
  [ -n "$kb" ] && [ -d "$kb" ] || return 1
  KB="$(cd "$kb" && pwd)" || return 1
  # A KB that IS this checkout is the legacy layout however it was named (a parent process may
  # have exported LEYDEN_HOME after resolving it).
  [ "$KB" = "$LDN_TOOL_ROOT" ] && src=legacy
  LEYDEN_HOME="$KB"
  LDN_STATE="$KB/.leyden"
  LDN_KB_SOURCE="$src"
  mkdir -p "$LDN_STATE" 2>/dev/null || true
  # Keep runtime state out of git even in a KB whose .gitignore predates .leyden/ (local exclude,
  # never committed, so a tool update cannot touch it).
  if [ -d "$KB/.git/info" ] && ! grep -qx '.leyden/' "$KB/.git/info/exclude" 2>/dev/null; then
    printf '%s\n' '.leyden/' >> "$KB/.git/info/exclude" 2>/dev/null || true
  fi
  export KB LEYDEN_HOME LDN_STATE LDN_KB_SOURCE
  return 0
}

ldn_no_kb_msg() { echo "leyden: no KB configured, run: leyden setup --personal local (or say \"set up leyden\" to Claude)" >&2; }

# ── Shared process/lock helpers (drain lock, git-op lock) ─────────────────────────────────────────
# ldn_mtime <path>: portable mtime in epoch seconds, empty if unreadable (GNU `stat -c`, BSD `stat -f`).
ldn_mtime() {
  local v
  v="$(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || true)"
  case "$v" in ""|*[!0-9]*) return 0 ;; *) printf '%s' "$v" ;; esac
}

# ldn_proc_start <pid>: epoch second the process started, empty if unknown. From `ps -o etime=`
# ([[dd-]hh:]mm:ss), which is the same on macOS and Linux (no locale-dependent date parsing).
ldn_proc_start() {
  local e d=0 h=0 m=0 s=0 rest
  e="$(ps -o etime= -p "$1" 2>/dev/null | tr -d ' ')"
  [ -n "$e" ] || return 0
  case "$e" in *-*) d="${e%%-*}"; e="${e#*-}" ;; esac
  rest="$e"
  case "$rest" in
    *:*:*) h="${rest%%:*}"; rest="${rest#*:}"; m="${rest%%:*}"; s="${rest#*:}" ;;
    *:*)   m="${rest%%:*}"; s="${rest#*:}" ;;
    *)     s="$rest" ;;
  esac
  case "$d$h$m$s" in *[!0-9]*) return 0 ;; esac
  printf '%s' "$(( $(date +%s) - (10#$d * 86400 + 10#$h * 3600 + 10#$m * 60 + 10#$s) ))"
}

# ldn_pid_alive <pid> [<ref-file>]: the pid is running AND (when a ref file is given) the process
# started no later than the ref file was written. A holder that died and whose pid was REUSED by an
# unrelated, newer process would otherwise look alive forever (the drain never runs again, the push
# waits its full timeout every session). 5 s of slack covers etime's one-second granularity.
ldn_pid_alive() {
  local p="$1" ref="${2:-}" st mt
  case "$p" in ""|*[!0-9]*) return 1 ;; esac
  kill -0 "$p" 2>/dev/null || return 1
  [ -n "$ref" ] && [ -e "$ref" ] || return 0
  st="$(ldn_proc_start "$p")"; mt="$(ldn_mtime "$ref")"
  { [ -n "$st" ] && [ -n "$mt" ]; } || return 0
  [ "$st" -le $((mt + 5)) ]
}

# ldn_lock_reap <lockdir> <stale-empty-age-s>: remove a lock whose holder is gone.
# Every pid file in the lock dir (pid, worker) is checked; a lock with no pid yet is only stale past
# <stale-empty-age-s> (the mkdir -> write-pid gap of a live holder). Reaping is SERIALISED by a
# second mutex ($lock.reaping): the old read-pid-then-rm let a waiter that had read a dead pid
# delete the lock another waiter had JUST taken (two holders inside, reproduced 2 of 15 runs), and
# a rename-then-restore cannot be made atomic (mv into an existing dir nests it). Under the mutex
# the lock is re-checked before removal; holders only ever CREATE the lock, and only reapers delete
# a dead one, so a lock judged dead under the mutex is still the same dead lock when it is removed.
ldn_lock_dead() {
  local lock="$1" emptyage="$2" p f any=0 now_e mt
  for f in "$lock/pid" "$lock/worker"; do
    [ -f "$f" ] || continue
    for p in $(cat "$f" 2>/dev/null); do
      case "$p" in ""|*[!0-9]*) continue ;; esac
      any=1
      ldn_pid_alive "$p" "$f" && return 1
    done
  done
  if [ "$any" -eq 0 ]; then
    now_e="$(date +%s)"; mt="$(ldn_mtime "$lock")"
    [ -n "$mt" ] || return 1
    [ $((now_e - mt)) -ge "$emptyage" ] || return 1
  fi
  return 0
}
ldn_lock_reap() {
  local lock="$1" emptyage="${2:-10}" now_e mt
  [ -d "$lock" ] || return 0
  ldn_lock_dead "$lock" "$emptyage" || return 0
  if ! mkdir "$lock.reaping" 2>/dev/null; then
    # Another process is reaping. A reaper killed mid-reap leaves its mutex behind: clear it after
    # 30 s so reaping cannot wedge forever (the next round re-checks everything).
    now_e="$(date +%s)"; mt="$(ldn_mtime "$lock.reaping")"
    [ -n "$mt" ] && [ $((now_e - mt)) -ge 30 ] && rm -rf "$lock.reaping" 2>/dev/null
    return 0
  fi
  if ldn_lock_dead "$lock" "$emptyage"; then
    rm -rf "$lock" 2>/dev/null || true
  fi
  rmdir "$lock.reaping" 2>/dev/null || rm -rf "$lock.reaping" 2>/dev/null || true
  return 0
}

# ── Health flags ──────────────────────────────────────────────────────────────────────────────────
# A background job that cannot do its work (push wedged, capture skipped mid-rebase) writes ONE
# human sentence to $LDN_STATE/health/<check>.flag and removes it when the condition clears. The
# SessionStart hook shows each flag as one line, so a stalled pipeline is visible in the session
# instead of only in capture.log.
ldn_health_set()   { mkdir -p "$LDN_STATE/health" 2>/dev/null && printf '%s\n' "$2" > "$LDN_STATE/health/$1.flag" 2>/dev/null; return 0; }
ldn_health_clear() { rm -f "$LDN_STATE/health/$1.flag" 2>/dev/null; return 0; }

# ldn_repo_busy <repo>: a rebase, merge, cherry-pick or detached HEAD is in progress. Committing
# then would land on a detached HEAD with conflict markers and wedge every later push.
ldn_repo_busy() {
  local g; g="$(git -C "$1" rev-parse --absolute-git-dir 2>/dev/null)" || return 1
  [ -d "$g/rebase-merge" ] || [ -d "$g/rebase-apply" ] || [ -f "$g/MERGE_HEAD" ] \
    || [ -f "$g/CHERRY_PICK_HEAD" ] || [ -f "$g/REVERT_HEAD" ] && return 0
  git -C "$1" symbolic-ref -q HEAD >/dev/null 2>&1 || return 0
  return 1
}

# Python flags for every interpreter call from a hook: -I (isolated: the cwd, PYTHONPATH and user
# site-packages are NOT on sys.path, so a project's own json.py can no longer shadow the stdlib and
# silently drop sessions) and -B (no __pycache__ writes).
LDN_PYFLAGS="-I -B"

LDN_KB_OK=0
if ldn_resolve_kb; then LDN_KB_OK=1; fi
true
