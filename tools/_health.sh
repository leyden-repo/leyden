# Leyden health signals, shared by the SessionStart hook (one short line) and `leyden doctor`
# (one WARN per signal). `source` this after tools/_kb.sh has set KB and LDN_STATE.
#
#   ldn_health        prints one plain sentence per problem, nothing when all is well
#
# Cheap on purpose: the hook calls it on every session start, so it reads files and asks git two
# questions. No python, no lint run, no network (budget well under 100 ms).
#
# Signals:
#   identity   git has no user.name / user.email: every background commit fails
#   push       commits waiting for the remote for more than 48 h
#   capture    sessions piling up in the capture queue, or the last capture outcome was a failure
#   secrets    files held back from git because they still contain a secret
#   lint       the last daily health record counted failing files (`leyden lint` says which)
#   flags      any one-sentence $LDN_STATE/health/*.flag a pipeline job left (removed by that job
#              when the condition clears)

ldn_epoch_of() {  # ldn_epoch_of <ISO-8601 UTC "YYYY-MM-DDTHH:MM:SSZ"> -> epoch seconds (BSD + GNU)
  date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null || date -u -d "$1" +%s 2>/dev/null || echo ""
}

ldn_health() {
  local now n name ahead last age log q qn last_line out lf flag f
  now="$(date +%s)"

  # identity: repo config wins over global, git resolves both
  # one git call for both keys (repo config wins over global, git resolves both)
  name="$(git -C "$KB" config --get-regexp '^user\.(name|email)$' 2>/dev/null | awk 'NF>1{print $1}' | sort -u | tr '\n' ' ')"
  if [ "$name" != "user.email user.name " ]; then
    echo "git has no user.name/user.email, so your notes cannot be committed. Fix: git config --global user.name \"Your Name\"; git config --global user.email you@example.com"
  fi

  # push: only when there is a remote to push to
  # ahead = commits not on any origin branch (one call; no remote = the call fails = 0)
  if [ -d "$KB/.git/refs/remotes/origin" ] || git -C "$KB" config --get remote.origin.url >/dev/null 2>&1; then
    ahead="$(git -C "$KB" rev-list --count HEAD --not --remotes=origin 2>/dev/null || echo 0)"
    ahead="${ahead:-0}"
    if [ "$ahead" -gt 0 ] 2>/dev/null; then
      last="$(cat "$LDN_STATE/.last-push" 2>/dev/null || true)"
      # no stamp yet (never pushed by Leyden): age of the OLDEST unpushed commit
      case "$last" in ''|*[!0-9]*) last="$(git -C "$KB" log --reverse --format=%ct HEAD --not --remotes=origin 2>/dev/null | head -n 1)" ;; esac
      age=$(( (now - ${last:-$now}) / 3600 ))
      if [ "$age" -ge 48 ]; then
        echo "$ahead commit(s) not pushed for ${age} h. Run: leyden push"
      fi
    fi
  fi

  # capture queue backlog and last outcome
  log="$LDN_STATE/capture.log"
  q="$LDN_STATE/capture-queue.txt"
  qn=0; [ -s "$q" ] && qn="$(grep -c . "$q" 2>/dev/null || echo 0)"
  if [ "$qn" -ge 5 ]; then
    echo "$qn sessions are waiting to be captured. Run: leyden charge (or leyden doctor if it keeps growing)"
  fi
  if [ -f "$log" ]; then
    last_line="$(tail -n 400 "$log" 2>/dev/null \
      | grep -E 'committed: |no changes|commit FAILED|ABANDONED|headless failed|nothing committed' | tail -n 1)"
    case "$last_line" in
      *"commit FAILED"*)      echo "the last capture could not be committed (see .leyden/capture.log). Run: leyden doctor" ;;
      *ABANDONED*)            echo "a session was given up on after repeated capture failures (see .leyden/capture.log)" ;;
      *"nothing committed"*)  echo "the last capture was held back because every file carried a secret (see .leyden/.secret-quarantine)" ;;
    esac
  fi

  # secrets held back
  # Unique paths still on disk (the commit guard prunes entries that now scan clean; no python here).
  n=0
  if [ -s "$LDN_STATE/.secret-quarantine" ]; then
    n="$(cut -f2 "$LDN_STATE/.secret-quarantine" | sort -u | while IFS= read -r f; do [ -n "$f" ] && [ -f "$KB/$f" ] && echo x; done | grep -c . || true)"
  fi
  if [ "${n:-0}" -gt 0 ] 2>/dev/null; then
    echo "$n file(s) are kept out of git because they contain a secret: remove the secret, then they commit on the next capture (list: .leyden/.secret-quarantine)"
  fi

  # lint, from the last daily health record (no lint run here)
  out="$(tail -n 1 "$LDN_STATE/metrics/health.log" 2>/dev/null || true)"
  lf="$(printf '%s' "$out" | sed -n 's/.*lint_fail=\([0-9][0-9]*\).*/\1/p')"
  if [ -n "$lf" ] && [ "$lf" -gt 0 ]; then
    echo "$lf file(s) fail lint (broken links or bad frontmatter). Run: leyden lint"
  fi

  # flags left by pipeline jobs
  if [ -d "$LDN_STATE/health" ]; then
    for f in "$LDN_STATE/health"/*.flag; do
      [ -f "$f" ] || continue
      flag="$(head -n 1 "$f" 2>/dev/null | cut -c1-600)"
      [ -n "$flag" ] && echo "$flag"
    done
  fi
  return 0
}
