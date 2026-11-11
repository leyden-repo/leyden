# Shared helpers for leyden hooks. `source` this; do not execute directly.
#
# Formerly tools/_secrets.sh. RENAMED because .gitignore's `*_secret*` rule matched that filename,
# so the one helper every hook sources was the one file never committed. On a fresh clone (which the
# offset store explicitly designs for: "dedup state travels with the repo for cross-machine
# continuity") both leyden-capture.sh and leyden-push-if-stale.sh sourced a missing file under
# `set -euo pipefail` and died on line 1, silently, forever. Do not rename this back.

# Matches PEM private keys and `<secret-ish-key>: <12+ char value>` assignments.
# Bare mentions in prose (no `: value`) do NOT match.
# NOTE: the PRIMARY detector is tools/leyden-redact.py (gitleaks-derived rule
# table + entropy fallback + pragma allowlist), which REDACTS in place so the entry still lands.
# This regex remains as the last-line backstop for files the redactor cannot rewrite (binaries,
# non-UTF-8): those keep the old unstage-and-quarantine response.
SECRET_RE='(-----BEGIN [A-Z ]*PRIVATE KEY-----|(secret|password|passwd|token|api[_-]?key|client[_-]?secret|cookie[_-]?secret|crypto[_-]?key)[[:space:]]*[:=][[:space:]]*[A-Za-z0-9/_+.=-]{12,})'

# Directory this file lives in (works when sourced): locates sibling tools like the redactor.
LEYDEN_TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Lock, health and repo-state helpers come from _kb.sh; every hook sources it first, but a caller
# that sources only this file still gets them.
declare -F ldn_lock_reap >/dev/null 2>&1 || . "$LEYDEN_TOOLS_DIR/_kb.sh"

# Interpreter for the sibling python tools: LEYDEN_PYTHON (same knob bin/leyden exports), else the
# python3 on PATH, else the absolute system python (hooks run with whatever PATH Claude Code hands
# them; Linux hosts may have no /usr/bin/python3 at all, macOS always does).
LEYDEN_PY="${LEYDEN_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"

# ── Taxonomy ──────────────────────────────────────────────────────────────────────────────────────
# Export LDN_ENTITY / LDN_ENTITIES / LDN_CONTEXT(S) / LDN_CASE(S) / LDN_PLAYBOOK(S) / LDN_INTERNAL /
# LDN_PROJECTS from leyden.yaml, once. Every directory name and every word in a log line or injected
# context comes from here, never from a literal, so a KB that calls its entities "accounts" and its
# cases "tickets" gets tooling that speaks the same words. Idempotent: an already-exported set (a
# caller that ran `_config.py shell` itself) is left alone.
ldn_load_taxonomy() {
  [ -n "${LDN_ENTITIES:-}" ] && return 0
  local exports
  exports="$("$LEYDEN_PY" "$LEYDEN_TOOLS_DIR/_config.py" shell 2>/dev/null || true)"
  if [ -n "$exports" ]; then
    eval "$exports"
  else
    # _config.py unavailable (broken python, half-cloned repo): fall back to the deployment preset
    # rather than dying in every hook. Matches _config.py's own "missing file is never an error".
    export LDN_ENTITY=customer LDN_ENTITIES=customers LDN_CONTEXT=deployment LDN_CONTEXTS=deployments
    export LDN_CASE=issue LDN_CASES=issues LDN_PLAYBOOK=runbook LDN_PLAYBOOKS=runbooks
    export LDN_INTERNAL=internal LDN_PROJECTS=projects
  fi
}
ldn_load_taxonomy

# ── Entity match pattern ──────────────────────────────────────────────────────────────────────────
# ldn_entity_pattern <KB_DIR> -> ERE alternation of entity slugs, WORD-BOUNDED.
# Shared so capture and reconcile cannot disagree about who counts as an entity. The folder list is
# read from <KB_DIR>/$LDN_ENTITIES (the configured entity directory), never from a literal name.
#
# The word boundaries are the point. A three-letter slug (say `abc`) matches inside any random
# string without \b: one real transcript produced 22 hits for such a slug, every one of them a
# substring of a base64 blob, and 0 real mentions. That alone was enough to keep that entity
# permanently in the reconcile gap list. `sed 's/-/[ -]/g'` keeps matching the spaced spelling
# ("acme corp" for slug `acme-corp`), which callers normalise back to the slug with `tr ' ' '-'`.
#
# Every name is ESCAPED before it joins the alternation, and the result must be valid in BOTH
# dialects that consume it: grep -E here and Python `re` in _transcript.py. One alias `c++corp` made
# grep exit 2 ("repetition-operator operand invalid"), the capture pre-filter came back empty, and
# every session was skipped with its offset committed. Names are first restricted to the characters
# _config.py allows in an alias (anything else is dropped), then `.` and `+` are bracket-quoted
# (`[.]`, `[+]` mean the same literal in both dialects, unlike backslash escapes).
ldn_entity_pattern() {
  local alt aliases
  alt="$(ls "$1/$LDN_ENTITIES" 2>/dev/null | grep -v '\.md$' | ldn_ere_names | paste -sd'|' - || true)"
  # Union in the registry aliases from leyden.yaml (short names, abbreviations, former names): a
  # session that refers to an entity ONLY by alias used to score zero in the capture pre-filter
  # (skipped, offset committed, permanently dropped) and the reconcile backstop was equally
  # alias-blind, so it could never flag the gap. The search hook already matched aliases; now all
  # three agree.
  aliases="$("$LEYDEN_PY" "$LEYDEN_TOOLS_DIR/_config.py" registry 2>/dev/null \
    | cut -f2 | tr '|' '\n' | ldn_ere_names | paste -sd'|' - || true)"
  [ -n "$aliases" ] && alt="${alt:+$alt|}$aliases"
  # The boundaries go through %s on purpose: bash's printf expands \b in a FORMAT string to an actual
  # backspace character (0x08), which silently produced a pattern with no word boundaries at all.
  # Hyphenated neighbours are part of the match: `\b` treats '-' as a boundary, so "acme-test"
  # (a different, possibly new entity) surfaced as "acme" and a real capture filed its work under
  # customers/acme/ and resolved acme's issue (found in testing). Now the match is "acme-test", which
  # is not a folder, so the worker is told about acme-test and creates it.
  if [ -z "$alt" ]; then printf '__none__'
  else printf '%s(%s)%s' '\b([A-Za-z0-9]+-)*(' "$alt" ')(-[A-Za-z0-9]+)*\b'; fi
}

# ldn_ere_names: filter, one name per line -> one ERE/Python-safe alternative per line (see above).
# The allowed set mirrors _config.py's _ALIAS_RE; `-` and ` ` both become `[ -]` so "acme corp"
# matches slug acme-corp and alias "ac me" matches "ac-me". Kept a separate function so capture, reconcile and any future caller escape identically.
ldn_ere_names() {
  grep -E "^[A-Za-z0-9][A-Za-z0-9 ._&+'-]*\$" | sed -e 's/[.+]/[&]/g' -e 's/[ -]/[ -]/g' || true
}

# ── Alias -> canonical slug ───────────────────────────────────────────────────────────────────────
# ldn_canonicalise: filter, stdin/stdout, one lowercase entity token per line -> canonical folder
# slug. MUST be applied to anything matched by ldn_entity_pattern before counting or deduping.
#
# The pattern deliberately matches folder slugs AND their registry aliases, and grep emits whichever
# alternative actually hit. Without this filter an entity referred to by its short name counts as a
# DIFFERENT entity from its own folder:
#   - leyden-reconcile.sh builds "logged" from folder slugs only, so every alias was permanently in
#     the gap list. On a KB that ran daily for four months, 3 of the 4 reported gaps were aliases of
#     entities whose log had been updated that same day, i.e. the backstop was mostly noise again,
#     the exact failure its KB-exclusion fix was meant to end.
#   - leyden-capture.sh hands the worker a "detected" hint list, so the same entity appeared twice
#     ("acme, acme-robotics") and the worker was told to evaluate each independently.
ldn_canonicalise() {
  local map
  map="$("$LEYDEN_PY" "$LEYDEN_TOOLS_DIR/_config.py" registry 2>/dev/null \
    | awk -F '\t' 'NF>1 && $2!="" {n=split($2,a,"|"); for(i=1;i<=n;i++) if(a[i]!="") print tolower(a[i])"\t"tolower($1)}')"
  [ -n "$map" ] || { cat; return 0; }
  # The map goes in as awk's FIRST FILE, not as -v: an assignment value is scanned for escape
  # sequences and a multi-line one aborts with "newline in string". Alias keys get the same
  # space->dash normalisation the callers apply to matched tokens, so a two-word alias
  # ("acme corp" for slug acme-corp) still resolves.
  awk -F '\t' '
    NR == FNR { k = $1; gsub(/ /, "-", k); if (k != "") m[k] = $2; next }
    { print ($0 in m) ? m[$0] : $0 }' <(printf '%s\n' "$map") -
}

# ldn_mtime, ldn_pid_alive and ldn_lock_reap live in tools/_kb.sh (sourced by every caller first).

# ── Git operation lock ────────────────────────────────────────────────────────────────────────────
# Two independent backgrounded jobs commit to this repo: the capture drain and push-if-stale, both
# launched from the same SessionStart hook. They raced, and capture.log records the result:
#   fatal: Unable to create '.../.git/index.lock': File exists.
# Worse than a lost commit: whichever job ran `git add -A` could stage the OTHER job's half-written
# files, and a secret hit in one used to wipe the other's in-flight work. Serialize instead.
# The lock stores its owner's pid so a killed holder is reaped instead of blocking forever. Reaping
# is the atomic rename in ldn_lock_reap: the old read-pid-then-rm let a waiter that had read a dead
# pid delete the lock another waiter had JUST taken (two holders inside, reproduced 2 of 15 runs).
ldn_git_lock() {
  local kb="$1" timeout="${2:-180}" lock waited=0
  lock="$kb/.leyden/.git-op.lock"
  while ! mkdir "$lock" 2>/dev/null; do
    ldn_lock_reap "$lock" 5
    # ALWAYS pay the wait before retrying: an unremovable lock (permission error, foreign uid) must
    # not become an unbounded 100% CPU spin that the timeout never bounds.
    waited=$((waited + 1))
    [ "$waited" -ge "$timeout" ] && return 1
    sleep 1
  done
  # $$ is correct here BECAUSE callers take this lock from the script's top-level process
  # (capture worker, push job). Do NOT call ldn_git_lock from inside a subshell: macOS bash 3.2
  # has no $BASHPID, and every "portable" substitute measured records a pid that is already dead.
  echo "$$" > "$lock/pid" 2>/dev/null || true
  return 0
}

# ldn_clear_stale_index_lock <repo> <log>: a git killed mid-operation (SIGKILL, power loss) leaves
# .git/index.lock behind, and every later `git add`/`commit` then fails with "File exists": captures
# committed their offset but never their files, and push logged "up to date" for 20 h. Called only
# while holding the git-op lock (so no Leyden git is running); the lock file is removed only when it
# is older than 2 minutes AND no git process is running against this repo.
ldn_clear_stale_index_lock() {
  local repo="$1" log="${2:-/dev/null}" g il now_e mt
  g="$(git -C "$repo" rev-parse --absolute-git-dir 2>/dev/null)" || return 0
  il="$g/index.lock"
  [ -f "$il" ] || return 0
  now_e="$(date +%s)"; mt="$(ldn_mtime "$il")"
  [ -n "$mt" ] && [ $((now_e - mt)) -ge 120 ] || return 0
  if ps -axo command= 2>/dev/null | grep -F -- "$repo" | grep -E '(^|/)git( |$)' | grep -v grep >/dev/null; then
    return 0
  fi
  rm -f "$il" 2>/dev/null && echo "$(date -u +%FT%TZ) removed stale .git/index.lock (age $((now_e - mt))s, no git running)" >> "$log"
  return 0
}

ldn_git_unlock() {
  rm -rf "$1/.leyden/.git-op.lock" 2>/dev/null || true
}

# ── Secret-safe commit ────────────────────────────────────────────────────────────────────────────
# ldn_commit_guard <KB_DIR> <COMMIT_MSG> <LOG> [LOCK_TIMEOUT_S]
# Stages everything, quarantines only the files that actually carry a secret, commits the rest.
# No-op (return 0) if nothing is staged. Returns 1 if a secret was found (whether or not the
# remainder committed), 2 if the git lock could not be acquired within LOCK_TIMEOUT_S (default 180).
#
# HISTORY: why this is surgical now. The previous implementation reacted to any secret hit with
#   git reset -q && git checkout -q -- . && git clean -fdq
# across the whole KB: every uncommitted edit reverted, every untracked file deleted. When the
# capture worker had just written an entry containing an API key, the guard destroyed that
# session's ENTIRE captured output. And because leyden-capture.sh commits the consumer offset
# before reaching the commit step, the transcript was never eligible for re-capture. That is the
# exact permanent-loss mode the offset store exists to prevent, defeated from the other end.
# Never revert or clean the tree here. Unstage the offending path and leave it on disk.
# ldn_is_code <repo-relative path>: true for Leyden's OWN code and scaffolding (tools/, bin/,
# templates/, team-template/, any _templates/ dir). The guard never rewrites or quarantines these:
# the redactor once rewrote a comment inside tools/leyden-redact.py itself, and its --selftest
# fixtures are fake secrets by design. Secret scanning is for knowledge, not for the tool source.
# Only in the LEGACY layout (KB == the tool checkout) do these paths hold tool code. In a split KB
# they are ordinary KB folders, and the exemption let a secret in `templates/acme.md` or
# `customers/x/_templates/notes.md` be committed raw (reproduced).
ldn_is_code() {
  [ "${LDN_KB_SOURCE:-}" = legacy ] || return 1
  case "$1" in
    tools/*|bin/*|templates/*|team-template/*|_templates/*|*/_templates/*) return 0 ;;
    *) return 1 ;;
  esac
}

# A caller that already holds the git-op lock (the push job holds ONE lock across commit, rebase
# and push) sets LDN_GIT_LOCK_HELD=1 so the guard neither takes nor releases it. The unlock is an
# explicit call, not `trap ... RETURN`: a RETURN trap set in a function outlives it at top level
# and fired an unlock of a lock taken later by the same script.
ldn_commit_guard() {
  local KB="$1" lock_timeout="${4:-180}" rc
  if [ "${LDN_GIT_LOCK_HELD:-0}" = 1 ]; then
    _ldn_commit_body "$@"; return $?
  fi
  if ! ldn_git_lock "$KB" "$lock_timeout"; then
    echo "$(date -u +%FT%TZ) commit skipped: git lock busy (another leyden job holds it)" >> "$3"
    return 2
  fi
  _ldn_commit_body "$@"; rc=$?
  ldn_git_unlock "$KB"
  return "$rc"
}

# ── Capture-only commits (LDN_COMMIT_ONLY=<NUL-delimited path list>) ────────────────────────────
# leyden-capture.sh passes the paths ITS worker created or changed. The guard then stamps, redacts,
# stages and commits only those (`git commit --only`), never a hand edit in progress, and refuses
# files Claude Code would load as instructions or config in a later session.
# Matched case-insensitively: APFS is, so `x/Claude.md` or `.Claude/settings.json` IS the file
# Claude Code loads, and the old case-sensitive patterns let them through (found in testing).
_ldn_lc() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
_ldn_instruction_file() {
  case "$(_ldn_lc "$1")" in
    claude.md|*/claude.md|claude.local.md|*/claude.local.md|.mcp.json|*/.mcp.json|.claude/*|*/.claude/*) return 0 ;;
  esac
  return 1
}
_ldn_protected() {
  _ldn_instruction_file "$1" && return 0
  case "$(_ldn_lc "$1")" in
    .git/*|.gitignore|.gitattributes|.gitmodules|.leyden/*|leyden.yaml|leyden.local.yaml|leyden-team.yaml|.kb-path) return 0 ;;
  esac
  return 1
}

# _ldn_undo_refused <KB> <path>: the capture list only holds paths that were CLEAN before the worker
# ran, so undoing the worker's change loses nothing of the user's. Tracked: restore from HEAD.
# New: move it to .leyden/refused/ for review. Left on disk, the next daily push (which stages
# everything) used to commit it anyway (found in testing: leyden.yaml and .gitattributes).
_ldn_undo_refused() {
  local kb="$1" f="$2" dest
  if git -C "$kb" cat-file -e "HEAD:$f" 2>/dev/null; then
    GIT_LITERAL_PATHSPECS=1 git -C "$kb" checkout -q HEAD -- "$f" 2>/dev/null && return 0
  fi
  [ -e "$kb/$f" ] || return 0
  dest="$kb/.leyden/refused/$(date -u +%Y%m%dT%H%M%SZ)/$f"
  mkdir -p "$(dirname "$dest")" && mv -f "$kb/$f" "$dest"
}

# _ldn_targets_z <KB> <only-list|"">: NUL list of staged paths this commit may include.
_ldn_targets_z() {
  local f
  if [ -z "$2" ]; then git -C "$1" -c core.quotePath=false diff --cached --name-only -z; return 0; fi
  while IFS= read -r -d '' f; do
    GIT_LITERAL_PATHSPECS=1 git -C "$1" diff --cached --quiet -- "$f" 2>/dev/null || printf '%s\0' "$f"
  done < "$2"
  return 0
}

# ldn_stamp_timestamps <KB> <path>...: set `timestamp:` in each .md's EXISTING frontmatter to now (UTC),
# so the value no longer depends on the worker model, which has no clock.
ldn_stamp_timestamps() {
  local kb="$1"; shift
  [ "$#" -gt 0 ] || return 0
  "$LEYDEN_PY" -I -B - "$kb" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$@" <<'PY' || true
import os, re, sys
kb, ts = sys.argv[1], sys.argv[2]
for rel in sys.argv[3:]:
    p = os.path.join(kb, rel)
    try:
        with open(p, encoding="utf-8", newline="") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        continue
    m = re.match(r"---\r?\n(.*?\r?\n)---[ \t]*(\r?\n|$)", text, re.S)
    if not m:
        continue                       # no frontmatter: the body is never touched
    fm = m.group(1)
    if re.search(r"(?m)^timestamp:", fm):
        new_fm = re.sub(r"(?m)^timestamp:[^\r\n]*", "timestamp: " + ts, fm, count=1)
    else:
        new_fm = fm + "timestamp: " + ts + ("\r\n" if fm.endswith("\r\n") else "\n")
    if new_fm != fm:
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(text[:m.start(1)] + new_fm + text[m.end(1):])
PY
}

# _ldn_quarantine_prune <quarantine> <KB>: one line per path, and only while the file still exists
# and still scans dirty, so the session-start warning clears once the secret is gone.
_ldn_quarantine_prune() {
  local q="$1" kb="$2" ts f
  [ -s "$q" ] || return 0
  awk -F '\t' 'NF >= 2 && !seen[$2]++' "$q" | while IFS=$'\t' read -r ts f; do
    [ -n "$f" ] && [ -f "$kb/$f" ] || continue
    LC_ALL=C tr -d '\000' < "$kb/$f" | "$LEYDEN_PY" "$LEYDEN_TOOLS_DIR/leyden-redact.py" --scan - >/dev/null 2>&1 && continue
    printf '%s\t%s\n' "$ts" "$f"
  done > "$q.tmp.$$" && mv "$q.tmp.$$" "$q"
  return 0
}

_ldn_commit_body() {
  local KB="$1" MSG="$2" LOG="$3" f found=0 staged quarantine out rrc only="" refused=""
  local -a stamp=()
  quarantine="$KB/.leyden/.secret-quarantine"

  # A rebase/merge in progress or a detached HEAD: committing now lands conflict-marked files on a
  # detached HEAD and wedges every later push. Leave the tree alone and say so where it is seen.
  if ldn_repo_busy "$KB"; then
    echo "$(date -u +%FT%TZ) commit skipped: KB has a rebase/merge in progress or a detached HEAD (finish it: git -C \"$KB\" status)" >> "$LOG"
    ldn_health_set repo-busy "Your KB has an unfinished git rebase or merge, so nothing is being committed or pushed. Run: git -C \"$KB\" status"
    return 3
  fi
  ldn_health_clear repo-busy
  ldn_clear_stale_index_lock "$KB" "$LOG"
  _ldn_quarantine_prune "$quarantine" "$KB"

  if [ -n "${LDN_COMMIT_ONLY:-}" ]; then
    # Capture commit: keep only the worker's paths that are not protected, then stamp their timestamps.
    only="$LDN_COMMIT_ONLY.ok"; : > "$only"
    while IFS= read -r -d '' f; do
      [ -n "$f" ] || continue
      if _ldn_protected "$f"; then refused="$refused $f"; _ldn_undo_refused "$KB" "$f"; continue; fi
      printf '%s\0' "$f" >> "$only"
      case "$f" in *.md) [ -f "$KB/$f" ] && ! ldn_is_code "$f" && stamp+=("$f") ;; esac
    done < "$LDN_COMMIT_ONLY"
    if [ -n "$refused" ]; then
      echo "$(date -u +%FT%TZ) capture REFUSED protected path(s), undone (restored from git, or moved to .leyden/refused/), not committed:$refused" >> "$LOG"
      ldn_health_set capture-protected "The capture worker tried to write protected files in your KB; Leyden undid it (new files are in $KB/.leyden/refused/ for review):$refused"
    else
      ldn_health_clear capture-protected
    fi
    [ -s "$only" ] || { rm -f "$only"; return 0; }
    ldn_stamp_timestamps "$KB" ${stamp[@]+"${stamp[@]}"}
  fi

  # Redact BEFORE the first `git add`: staging writes the raw blob into .git/objects, where the
  # secret survived as an unreachable object until gc even after the redacted file was restaged
  # (found in testing). The pass after the add below stays as a second line of defence.
  while IFS= read -r -d '' f; do
    [ -f "$KB/$f" ] || continue
    case "$f" in *.md) ;; *) continue ;; esac
    ldn_is_code "$f" && continue
    out="$("$LEYDEN_PY" "$LEYDEN_TOOLS_DIR/leyden-redact.py" --redact "$KB/$f" 2>>"$LOG")"; rrc=$?
    if [ "$rrc" -eq 1 ] && [ -n "$out" ]; then
      found=1
      printf '%s\n' "$out" | while IFS= read -r hit; do
        echo "$(date -u +%FT%TZ) REDACTED in $f: $hit" >> "$LOG"
        printf '%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$f" "$hit" >> "$KB/.leyden/redactions.log"
      done
    elif [ "$rrc" -gt 1 ]; then
      # The redactor itself failed: fail closed. The backstop below re-scans and unstages.
      echo "$(date -u +%FT%TZ) redactor error (rc=$rrc) on $f: left for the backstop scan" >> "$LOG"
    fi
  done < <(if [ -n "$only" ]; then cat "$only"; else git -C "$KB" -c core.quotePath=false ls-files -z --modified --others --exclude-standard; fi)

  # Runtime state (.leyden/) never stages: the KB .gitignore lists it, and tools/_kb.sh also adds it
  # to .git/info/exclude on every run (a KB whose .gitignore predates it). An explicit
  # ':(exclude).leyden' pathspec is NOT used: git add then exits 1 ("paths are ignored").
  if [ -n "$only" ]; then
    rrc=0; GIT_LITERAL_PATHSPECS=1 git -C "$KB" add -A --pathspec-from-file="$only" --pathspec-file-nul 2>>"$LOG" || rrc=1
  else
    rrc=0; git -C "$KB" add -A 2>>"$LOG" || rrc=1
    # The daily push stages everything, so also keep out files Claude Code would load as instructions
    # or config (a CLAUDE.md the capture worker was refused would otherwise ride along here).
    if [ "$rrc" -eq 0 ]; then
      local pf
      ldn_health_clear push-protected   # set again below if a protected file is still staged
      while IFS= read -r -d '' pf; do
        case "$pf" in
          workspace-config/*) ;;   # Leyden's own backup mirror of the workspace's .claude/ and CLAUDE.md
          *) _ldn_instruction_file "$pf" || [ "$(_ldn_lc "$pf")" = .gitattributes ] || [ "$(_ldn_lc "$pf")" = .gitmodules ] || continue
            GIT_LITERAL_PATHSPECS=1 git -C "$KB" reset -q -- "$pf" 2>/dev/null \
              || GIT_LITERAL_PATHSPECS=1 git -C "$KB" rm -q --cached -- "$pf" 2>>"$LOG" || true   # no HEAD yet
            echo "$(date -u +%FT%TZ) REFUSED (instruction/config file, not committed): $pf" >> "$LOG"
            ldn_health_set push-protected "Leyden kept $pf out of git: files Claude Code loads as instructions do not belong in the KB. Delete it or move it out." ;;
        esac
      done < <(git -C "$KB" -c core.quotePath=false diff --cached --name-only -z --diff-filter=d 2>/dev/null)
    fi
  fi
  if [ "$rrc" -ne 0 ]; then
    echo "$(date -u +%FT%TZ) commit FAILED: git add -A failed (see the git error above): $MSG" >> "$LOG"
    ldn_health_set commit-failed "Leyden could not stage files in your KB (git add failed); see $KB/.leyden/capture.log"
    [ -n "$only" ] && rm -f "$only"
    return 4
  fi
  if [ -z "$(_ldn_targets_z "$KB" "$only" | tr -d '\000')" ]; then [ -n "$only" ] && rm -f "$only"; return "$found"; fi

  # PRIMARY: redact-in-place (leyden-redact.py). The secret SPAN becomes
  # <REDACTED:<rule>:sha256/<8>> and the file is restaged: the knowledge lands, the value
  # never does, and the hash prefix identifies which credential leaked for rotation.
  # The redactor skips binaries/non-UTF-8 untouched; those fall through to the backstop below.
  # Only KB CONTENT is rewritten: *.md outside Leyden's own code (ldn_is_code). Config and other
  # non-markdown files are never edited in place; the backstop below still guards them.
  # NUL-delimited, quotePath off: git otherwise prints a non-ASCII name C-quoted
  # ("notes-caf\303\251.txt"), every [ -f ] and pathspec below missed it, and the backstop scanned an
  # EMPTY diff and let the secret through (reproduced with notes-café.txt).
  while IFS= read -r -d '' f; do
    [ -n "$f" ] || continue
    [ -f "$KB/$f" ] || continue           # deletions have nothing to redact
    case "$f" in *.md) ;; *) continue ;; esac
    ldn_is_code "$f" && continue
    out="$("$LEYDEN_PY" "$LEYDEN_TOOLS_DIR/leyden-redact.py" --redact "$KB/$f" 2>>"$LOG")"
    if [ "$?" -eq 1 ] && [ -n "$out" ]; then
      found=1
      printf '%s\n' "$out" | while IFS= read -r hit; do
        echo "$(date -u +%FT%TZ) REDACTED in $f: $hit" >> "$LOG"
        printf '%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$f" "$hit" >> "$KB/.leyden/redactions.log"
      done
      GIT_LITERAL_PATHSPECS=1 git -C "$KB" add -- "$f" 2>>"$LOG" || true
    fi
  done < <(_ldn_targets_z "$KB" "$only")

  # BACKSTOP: anything whose staged diff STILL carries a secret (binary, non-UTF-8, or a
  # redaction that failed) keeps the old surgical response: unstage that file only, leave it
  # on disk, quarantine-list it. Nothing is ever reverted or deleted.
  # Two exclusions, or the backstop eats the redactor's own output: strip <REDACTED:...>
  # markers first (the marker text "sas-token:sha256/xx" itself matches SECRET_RE), and skip
  # `pragma: allowlist secret` lines (deliberate doc examples the redactor already honoured).
  while IFS= read -r -d '' f; do
    [ -n "$f" ] || continue
    ldn_is_code "$f" && continue          # tool source: see ldn_is_code
    # The backstop scans with the SAME detector as the primary path (leyden-redact.py --scan on
    # stdin) instead of the weaker SECRET_RE copy, which missed AWS/Slack/GitHub token formats.
    # Only ADDED lines are scanned (-U0, '^+'): a commit that REMOVES a secret must not be
    # quarantined for it. The scanner reads stdin to EOF (no -q/SIGPIPE hazard), handles pragma
    # lines and existing <REDACTED:> markers itself, and any scanner ERROR also lands in the
    # quarantine branch, failing closed, never open. grep is ||true-wrapped: zero added lines
    # is a legitimate diff, not a finding, and must not trip pipefail.
    # --text: a binary file (NUL bytes) is diffed as text, so a token inside it is scanned too
    # (git otherwise prints only "Binary files differ" and the scan saw nothing; reproduced).
    if GIT_LITERAL_PATHSPECS=1 git -C "$KB" -c core.quotePath=false diff --cached --text -U0 -- "$f" 2>>"$LOG" \
         | LC_ALL=C tr -d '\000' \
         | { LC_ALL=C grep -aE '^\+' || true; } \
         | "$LEYDEN_PY" "$LEYDEN_TOOLS_DIR/leyden-redact.py" --scan - >/dev/null 2>&1; then
      :
    else
      found=1
      echo "$(date -u +%FT%TZ) SECRET in $f: unstaged, left on disk, NOT committed" >> "$LOG"
      # one line per path: a file already listed is not appended again (the health count was doubling)
      LDN_QF="$f" awk -F '\t' '$2 == ENVIRON["LDN_QF"] { hit = 1 } END { exit !hit }' "$quarantine" 2>/dev/null \
        || printf '%s\t%s\n' "$(date -u +%FT%TZ)" "$f" >> "$quarantine"
      GIT_LITERAL_PATHSPECS=1 git -C "$KB" reset -q -- "$f" 2>>"$LOG" || true
    fi
  done < <(_ldn_targets_z "$KB" "$only")

  if [ -n "$only" ]; then
    _ldn_targets_z "$KB" "$only" > "$only.final"; mv "$only.final" "$only"
    staged="$(tr -cd '\000' < "$only" | wc -c | tr -d ' ')"
  else
    staged="$(git -C "$KB" -c core.quotePath=false diff --cached --name-only | grep -c . || true)"
  fi
  if [ "${staged:-0}" -eq 0 ]; then
    [ "$found" -eq 1 ] && echo "$(date -u +%FT%TZ) nothing committed: every staged file carried a secret (see .leyden/.secret-quarantine)" >> "$LOG"
    [ -n "$only" ] && rm -f "$only"
    return "$found"
  fi

  if [ -n "$only" ]; then
    # --only: commit exactly these paths; anything the user staged by hand stays staged, uncommitted.
    rrc=0; GIT_LITERAL_PATHSPECS=1 git -C "$KB" commit -q -m "$MSG" --only --pathspec-from-file="$only" --pathspec-file-nul 2>>"$LOG" || rrc=1
    rm -f "$only"
  else
    rrc=0; git -C "$KB" commit -q -m "$MSG" 2>>"$LOG" || rrc=1
  fi
  if [ "$rrc" -eq 0 ]; then
    echo "$(date -u +%FT%TZ) committed: $MSG ($staged files)" >> "$LOG"
    ldn_health_clear commit-failed
  else
    echo "$(date -u +%FT%TZ) commit FAILED: $MSG" >> "$LOG"
    ldn_health_set commit-failed "Leyden could not commit to your KB (often: no git user.name/user.email set). See $KB/.leyden/capture.log"
    return 4
  fi
  return "$found"
}
