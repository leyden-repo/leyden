#!/usr/bin/env bash
# leyden doctor: verify a clone is a working install. Setup itself is `leyden init` (tools/leyden-init.py).
#
#   leyden doctor                      # PASS/FAIL per item, exit 1 on any FAIL
#   leyden doctor --hooks              # also print the hooks block to paste when they are missing
#   leyden doctor --settings <file>    # check the hooks in this settings.json (repeatable)
# Non-destructive by design: never edits .claude/settings.json (that file carries the user's own
# permissions config: print what to add, let a human paste it).
set -uo pipefail

LDN_NO_KB=0
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kb.sh"; [ "$LDN_KB_OK" = 1 ] || LDN_NO_KB=1   # KB, LDN_TOOLS, LDN_STATE (tools/_kb.sh)
if [ "$LDN_NO_KB" = 1 ]; then
  echo "leyden doctor: tool $LDN_TOOL_ROOT"
  if [ -f "$LDN_TOOL_ROOT/.kb-path" ] && [ -z "${LEYDEN_HOME:-}" ]; then
    echo "FAIL  KB folder missing: $LDN_TOOL_ROOT/.kb-path points at '$(head -n 1 "$LDN_TOOL_ROOT/.kb-path")' (relative to $(dirname "$LDN_TOOL_ROOT")), which does not exist"
    echo "      fix: move the KB folder back there, or re-run setup to clone it from your private repo"
  else
    echo "FAIL  no KB configured: no LEYDEN_HOME and no $LDN_TOOL_ROOT/.kb-path"
    echo "      fix: leyden init   (in a terminal: creates your KB next to the tool and records its path)"
  fi
  echo "           scripted: leyden init --kb <path> --write-hooks <workspace>/.claude/settings.json --yes"
  echo
  echo "doctor: 1 check(s) failed"
  exit 1
fi
WS="$(cd "$KB/.." && pwd)"
# Same interpreter knob as bin/leyden, _guard.sh and every shell tool. Doctor used to hardcode
# /usr/bin/python3, so on an install that sets LEYDEN_PYTHON it validated FTS5, the index build,
# the lint and the search hook against an interpreter the hooks never use: it could report PASS
# for a broken install, or FAIL for a working one.
PY="${LEYDEN_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"
fails=0

SHOW_HOOKS=0
SETTINGS_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --hooks) SHOW_HOOKS=1 ;;
    --settings) shift; [ -n "${1:-}" ] && SETTINGS_ARGS+=("$1") ;;
    --settings=*) SETTINGS_ARGS+=("${1#--settings=}") ;;
    -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
  shift
done

check() {  # check <label> <command...>
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf 'PASS  %s\n' "$label"
  else
    printf 'FAIL  %s\n' "$label"
    fails=$((fails + 1))
  fi
}

echo "leyden doctor: KB $KB (from: $LDN_KB_SOURCE), tool $LDN_TOOL_ROOT"
# Tool and KB are separate repos: the tool is pull-only, the KB is your private repo. A KB inside
# the tool checkout would be pushed with the tool, or wiped by a tool update.
if [ "$LDN_KB_SOURCE" = legacy ]; then
  echo "WARN  legacy layout: the KB lives inside the tool checkout. Move it to its own repo:"
  echo "      leyden init --kb <new-path>, copy your folders over, then delete them from the tool checkout"
else
  case "$KB/" in
    "$LDN_TOOL_ROOT/"*) echo "FAIL  KB is inside the tool repo ($LDN_TOOL_ROOT): run leyden init --kb <path outside it>"
                        fails=$((fails + 1)) ;;
    *) echo "PASS  KB is a separate folder from the tool" ;;
  esac
fi
check "runtime state dir writable ($LDN_STATE)" test -d "$LDN_STATE" -a -w "$LDN_STATE"
check "taxonomy valid (slot names lowercase-kebab, distinct)" "$PY" "$LDN_TOOLS/_config.py" validate
check "bash present (3.2+ ok; \$BASHPID not required)" bash -c 'true'
check "python3 present ($PY)" "$PY" -I -B -c 'pass'
check "sqlite FTS5 available (search index)" "$PY" -I -B -c \
  "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(a)')"
check "KB is a git repo" test -e "$KB/.git"
# Identity: every background commit (capture, push cycle, sweep apply) needs it. Without it they all
# fail with "Author identity unknown" while every other check here passes, and nothing is ever saved.
gname="$(git -C "$KB" config user.name 2>/dev/null || true)"
gmail="$(git -C "$KB" config user.email 2>/dev/null || true)"
if [ -n "$gname" ] && [ -n "$gmail" ]; then
  echo "PASS  git identity set ($gname <$gmail>)"
else
  echo "FAIL  git has no user.name/user.email: Leyden cannot commit your notes"
  echo "      fix: git config --global user.name \"Your Name\""
  echo "           git config --global user.email you@example.com"
  fails=$((fails + 1))
fi
# The claude CLI runs the background capture and the weekly sweep. Recall works without it, so a
# missing CLI used to pass here while the KB silently stopped filling.
CLAUDE_FOUND="$(command -v claude 2>/dev/null || true)"
[ -n "$CLAUDE_FOUND" ] || { [ -x "$HOME/.local/bin/claude" ] && CLAUDE_FOUND="$HOME/.local/bin/claude"; }
if [ -n "$CLAUDE_FOUND" ]; then
  echo "PASS  claude CLI found ($CLAUDE_FOUND)"
else
  echo "FAIL  claude CLI not found: capture and the weekly sweep need it"
  echo "      fix: install Claude Code (https://claude.com/claude-code), then check that 'claude --version' runs in this shell"
  fails=$((fails + 1))
fi
# A remote is optional (the docs say so): without one the KB lives only on this laptop.
if git -C "$KB" remote get-url origin >/dev/null 2>&1; then
  echo "PASS  git remote configured (auto-push target)"
  # A remote that does not exist used to PASS here while every push failed in the background.
  rout="$("$PY" -I -B "$LDN_TOOLS/leyden-init.py" --check-remote 2>&1)"; rrc=$?
  case "$rrc" in
    0) echo "PASS  git remote reachable" ;;
    1) echo "FAIL  git remote does not exist: $rout"
       echo "      fix: create that EMPTY private repo, or: git -C \"$KB\" remote set-url origin <url>"
       fails=$((fails + 1)) ;;
    *) echo "WARN  could not reach the git remote (offline? no access?): $rout" ;;
  esac
else
  echo "WARN  no git remote: your KB exists only on this laptop. Add a PRIVATE repo: git -C \"$KB\" remote add origin <url>"
fi
# A clone of the tool repo starts with origin = the TOOL repo; the batched push would publish the
# whole KB (entities, leyden.yaml with your email) there. `leyden init` moves it out of the way.
if problem="$("$PY" "$LDN_TOOLS/leyden-init.py" --check-origin 2>&1)"; then
  echo "PASS  origin is not the Leyden tool repo"
else
  echo "FAIL  ${problem:-origin check failed to run}"
  fails=$((fails + 1))
fi
check "core tools present" test -f "$LDN_TOOLS/_guard.sh" -a -f "$LDN_TOOLS/leyden-redact.py" \
  -a -f "$LDN_TOOLS/leyden-index.py" -a -f "$LDN_TOOLS/_config.py"
if [ -f "$KB/leyden.yaml" ]; then
  echo "PASS  leyden.yaml present"
elif [ -f "$KB/config.yaml" ]; then
  echo "WARN  only a legacy config.yaml (read as a fallback). Replace it with leyden.yaml (see leyden.yaml.example)"
else
  echo "FAIL  no leyden.yaml in the KB"
  echo "      fix: leyden init --kb \"$KB\" --yes"
  fails=$((fails + 1))
fi
check "index builds" "$PY" "$LDN_TOOLS/leyden-index.py"
# Paths go in as positional arguments, never spliced into the -c string: a KB path with a quote or
# space in it broke these checks (or made them test the wrong thing).
check "lint parses the KB (FAILs are content, not install)" \
  bash -c '"$1" "$2" >/dev/null 2>&1; [ $? -le 1 ]' _ "$PY" "$LDN_TOOLS/leyden-lint.py"
# The hook is checked MECHANICALLY (exit 0, and its output is empty or valid hook JSON), not for
# content: it used to query a fixed phrase and require a hit, which FAILed on every fresh KB.
check "search hook runs (exit 0, valid JSON or silent)" bash -c '
  out="$(printf "%s" "{\"prompt\":\"leyden doctor probe query\"}" | LEYDEN_TAP=1 "$1")" || exit 1
  [ -z "$out" ] || printf "%s" "$out" | "$2" -I -B -c "import json,sys; d=json.load(sys.stdin); assert \"additionalContext\" in d[\"hookSpecificOutput\"]"
' _ "$LDN_TOOLS/leyden-search.sh" "$PY"
check "brief dashboard generates" bash -c '"$1" "$2" --brief | grep -q open' _ "$PY" "$LDN_TOOLS/leyden-issues.py"

# ── Hooks ─────────────────────────────────────────────────────────────────────────────────────
# Candidate settings files: the file `leyden init --write-hooks` recorded (git config), the
# project Claude Code is running in ($CLAUDE_PROJECT_DIR), the KB's parent workspace, and the KB
# itself; project and local variants of each. The first one that registers all three hooks is
# checked; it used to look only at <KB parent>/.claude/settings.json.
# With --settings, only the named files are checked.
CANDIDATES=()
if [ "${#SETTINGS_ARGS[@]}" -gt 0 ]; then
  CANDIDATES=("${SETTINGS_ARGS[@]}")
else
  rec="$(git -C "$KB" config --get leyden.hooksSettings 2>/dev/null || true)"
  # init records it relative to the KB (a copied workspace then checks its own file, not the original's)
  case "$rec" in ""|/*) ;; *) rec="$(cd "$KB/$(dirname "$rec")" 2>/dev/null && pwd)/$(basename "$rec")" ;; esac
  [ -n "$rec" ] && CANDIDATES+=("$rec")
  for d in ${CLAUDE_PROJECT_DIR:+"$CLAUDE_PROJECT_DIR"} "$WS" "$KB"; do
    CANDIDATES+=("$d/.claude/settings.json" "$d/.claude/settings.local.json")
  done
  CANDIDATES+=("$HOME/.claude/settings.json")
fi

# hook_report <settings>: for each of the three hooks, find its command and check that it
# RESOLVES the way a shell would run it: quotes honoured, $CLAUDE_PROJECT_DIR expanded to the
# project the file belongs to, first word an executable file of THIS clone. An unquoted path with
# a space in it failed every hook (exit 126) while this check, a plain grep, said PASS.
hook_report() {
  "$PY" -I -B - "$1" "$LDN_TOOLS" "$KB" <<'PY'
import json, os, shlex, sys
settings, tools, kb = sys.argv[1], os.path.realpath(sys.argv[2]), os.path.realpath(sys.argv[3])
try:
    data = json.load(open(settings, encoding="utf-8"))
except Exception as e:
    print(f"MISSING unreadable ({e.__class__.__name__})"); sys.exit(1)
sdir = os.path.dirname(os.path.abspath(settings))
proj = os.path.dirname(sdir) if os.path.basename(sdir) == ".claude" else os.getcwd()
env = dict(os.environ, CLAUDE_PROJECT_DIR=os.environ.get("CLAUDE_PROJECT_DIR") or proj)
bad = 0
for event, script in (("SessionStart", "leyden-load.sh"), ("UserPromptSubmit", "leyden-search.sh"),
                      ("SessionEnd", "leyden-enqueue.sh")):
    cmds = [h.get("command", "") for e in (data.get("hooks") or {}).get(event, []) if isinstance(e, dict)
            for h in e.get("hooks", []) if isinstance(h, dict) and script in h.get("command", "")]
    if not cmds:
        print(f"MISSING {event}: no {script} hook"); bad = 1; continue
    try:
        # Hook commands carry the KB as a leading assignment (`LEYDEN_HOME=<kb> LDN_KB_SOURCE=hook
        # <script>`) so each workspace's hooks serve that workspace's KB: skip VAR=value words.
        words = shlex.split(cmds[0])
        assigned = {}
        while words and "=" in words[0] and not words[0].startswith(("/", ".", "$", "~")) \
                and words[0].split("=", 1)[0].replace("_", "").isalnum():
            k, v = words.pop(0).split("=", 1)
            assigned[k] = v.replace("$CLAUDE_PROJECT_DIR", env["CLAUDE_PROJECT_DIR"]) \
                .replace("${CLAUDE_PROJECT_DIR}", env["CLAUDE_PROJECT_DIR"])   # init writes it project-relative
        word = words[0]
        path = os.path.expandvars(word.replace("$CLAUDE_PROJECT_DIR", env["CLAUDE_PROJECT_DIR"])
                                  .replace("${CLAUDE_PROJECT_DIR}", env["CLAUDE_PROJECT_DIR"]))
    except Exception:
        print(f"BROKEN {event}: cannot parse command {cmds[0]!r}"); bad = 1; continue
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        print(f"BROKEN {event}: {cmds[0]!r} does not resolve to an executable (unquoted space?)"); bad = 1
    elif os.path.realpath(path) != os.path.join(tools, script):
        print(f"BROKEN {event}: {cmds[0]!r} runs another clone ({path})"); bad = 1
    elif assigned.get("LEYDEN_HOME") and os.path.realpath(os.path.expanduser(assigned["LEYDEN_HOME"])) != kb:
        print(f"BROKEN {event}: serves another KB ({assigned['LEYDEN_HOME']}), not {kb}"); bad = 1
    if len(cmds) > 1:
        print(f"BROKEN {event}: {len(cmds)} {script} hooks registered (rerun leyden init --write-hooks)"); bad = 1
sys.exit(bad)
PY
}

SETTINGS="" report="" first_report="" first=""
for f in "${CANDIDATES[@]}"; do
  [ -f "$f" ] || continue
  grep -q "leyden-search.sh" "$f" 2>/dev/null || continue
  if report="$(hook_report "$f")"; then SETTINGS="$f"; break; fi
  [ -n "$first" ] || { first="$f"; first_report="$report"; }
done
if [ -n "$SETTINGS" ]; then
  echo "PASS  hooks registered and resolvable in $SETTINGS"
else
  fails=$((fails + 1))
  if [ -n "$first" ]; then
    echo "FAIL  hooks in $first do not work:"
    printf '%s\n' "$first_report" | sed 's/^/        /'
  else
    echo "FAIL  hooks not registered (looked in: ${CANDIDATES[*]})"
  fi
  target="${first:-${SETTINGS_ARGS[0]:-$WS/.claude/settings.json}}"
  if [ "$SHOW_HOOKS" -eq 1 ]; then
    echo
    echo "Add to $target under \"hooks\" (or run: leyden init --write-hooks $target):"
    "$PY" "$LDN_TOOLS/leyden-init.py" --print-hooks "$target"
  else
    echo "      fix: leyden init --write-hooks $target   (or leyden doctor --hooks to print the block)"
  fi
fi

# The /kb-sweep skill must be Leyden's own copy (a link to $LDN_TOOL_ROOT/skills/kb-sweep), not a
# same-name skill from another toolchain, or the sweep follows that toolchain's paths.
for d in ${CLAUDE_PROJECT_DIR:+"$CLAUDE_PROJECT_DIR"} "$WS"; do
  sk="$d/.claude/skills/kb-sweep"
  [ -e "$sk" ] || continue
  if [ "$(cd "$sk" 2>/dev/null && pwd -P)" = "$(cd "$LDN_TOOL_ROOT/skills/kb-sweep" 2>/dev/null && pwd -P)" ]; then
    echo "PASS  /kb-sweep skill is Leyden's ($sk)"
  else
    echo "WARN  $sk is not Leyden's kb-sweep skill (another toolchain's copy?): /kb-sweep would run that one"
    echo "      fix: move it aside, then: ln -s \"$LDN_TOOL_ROOT/skills/kb-sweep\" \"$sk\""
  fi
  break
done

# ── Pipeline health (recency, backlog, failures): WARN only, the install itself can be fine ──
# shellcheck source=/dev/null
. "$LDN_TOOLS/_health.sh"
hw=0
while IFS= read -r line; do
  [ -n "$line" ] || continue
  case "$line" in "git has no user"*) continue ;; esac   # already a FAIL above
  echo "WARN  $line"; hw=$((hw + 1))
done < <(ldn_health 2>/dev/null)
# Last capture run: a KB whose sessions keep ending but never get captured is the silent failure.
last_cap="$(grep -E 'capture start:|capture skip' "$LDN_STATE/capture.log" 2>/dev/null | tail -n 1 | cut -c1-20)"
if [ -n "$last_cap" ]; then
  ce="$(ldn_epoch_of "$last_cap")"
  if [ -n "$ce" ]; then
    cd_days=$(( ($(date +%s) - ce) / 86400 ))
    if [ "$cd_days" -ge 7 ]; then
      echo "WARN  no capture has run for $cd_days days (last: $last_cap). If you used Claude since, check the SessionEnd hook, then run: leyden charge"
      hw=$((hw + 1))
    else
      echo "PASS  last capture ran $last_cap"
    fi
  fi
else
  echo "INFO  no capture has run yet (normal on a new install: it runs at the start of the session after next)"
fi
[ "$hw" -eq 0 ] && echo "PASS  pipeline health (push, capture queue, secrets, lint)"

# Copied workspace: .leyden/ (machine-local) remembers where this KB lived. If that folder still
# exists and pushes to the same remote, this is a copy and the two KBs will fight over one remote.
if [ -n "${KB:-}" ] && [ -d "$KB" ]; then
  HERE_KB="$(cd "$KB" && pwd -P)"; MARK="$LDN_STATE/kb-home"
  WAS="$(cat "$MARK" 2>/dev/null || true)"
  ORIGIN="$(git -C "$KB" remote get-url origin 2>/dev/null || true)"
  if [ -n "$WAS" ] && [ "$WAS" != "$HERE_KB" ] && [ -d "$WAS/.git" ] && [ -n "$ORIGIN" ] \
     && [ "$(git -C "$WAS" remote get-url origin 2>/dev/null)" = "$ORIGIN" ]; then
    echo "WARN  this KB is a copy of $WAS and pushes to the same remote; give one of them its own: git -C \"$KB\" remote set-url origin <new-url> && git -C \"$KB\" push -u origin HEAD"
  else
    printf '%s\n' "$HERE_KB" > "$MARK" 2>/dev/null || true   # first run, or a move: remember the new home
  fi
fi

# Team repo: configured but missing (moved, deleted) means team search silently goes stale.
TEAMDIR="$(cd "$KB" && "$PY" -I -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import importlib.util as u
spec = u.spec_from_file_location("t", sys.argv[1] + "/leyden-team.py"); t = u.module_from_spec(spec); spec.loader.exec_module(t)
p = t.shared_dir(); print(p or "")' "$LDN_TOOLS" 2>/dev/null)"
if [ -n "$TEAMDIR" ]; then
  if [ -d "$TEAMDIR/.git" ]; then
    echo "PASS  team repo $TEAMDIR"
  else
    echo "FAIL  team repo $TEAMDIR is missing (moved or deleted). Fix: leyden team join <team-repo-url> --yes"
    fails=$((fails + 1))
  fi
fi

echo
if [ "$fails" -eq 0 ]; then echo "doctor: no failures (any WARN above is advice, not a fault)"; else echo "doctor: $fails check(s) failed"; fi
exit "$([ "$fails" -eq 0 ] && echo 0 || echo 1)"
