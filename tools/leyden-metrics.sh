#!/usr/bin/env bash
# leyden metrics + health tracker.
# Reads signals the pipeline ALREADY emits (capture.log timestamped markers, git history
# auto-log commits, the offset store, and a live reconcile) and turns them into an effectiveness
# report + PASS/WARN/FAIL health checks. No new hot-path instrumentation; purely a reader.
#
# Modes:
#   leyden-metrics.sh                 full report over the last 14 days (default)
#   leyden-metrics.sh --report [N]    full report over the last N days
#   leyden-metrics.sh --daily         quiet: append ONE dated line to metrics/health.log (once/day).
#                                     Called from leyden-load.sh so trend accrues passively.
# Exit code (report mode): 0 all pass · 3 warnings · 4 failures  (so cron/alerts can branch).
set -uo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kb.sh"; [ "$LDN_KB_OK" = 1 ] || exit 0   # KB, LDN_TOOLS, LDN_STATE (tools/_kb.sh)
# Vocabulary for the report wording (entity word), from leyden.yaml via _config.py.
PY="${LEYDEN_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"
eval "$("$PY" "$LDN_TOOLS/_config.py" shell 2>/dev/null)" || true
ENT="${LDN_ENTITY:-customer}"; ENTS="${LDN_ENTITIES:-customers}"
LOG="$LDN_STATE/capture.log"
OFFSETS="$LDN_STATE/offsets/capture.tsv"
QUEUE="$LDN_STATE/capture-queue.txt"
LOCK="$LDN_STATE/.capture.lock"
HEALTH="$LDN_STATE/metrics/health.log"
COST="$LDN_STATE/cost.log"   # one line per headless run, TAB-separated:
                             # <ISO time> <capture|sweep> model=.. cost_usd=.. in=.. out=.. cache_read=.. ...
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
cutoff_date() { date -u -v-"$1"d +%Y-%m-%d 2>/dev/null || date -u -d "-$1 days" +%Y-%m-%d; }

# ---- shared counters over a window (dates >= cutoff), parsed from capture.log ----
compute() {
  local since="$1"
  STARTED=0 FAILED=0 NOCHANGE=0 SKIP=0 PUSHES=0 LINTFAIL=0
  if [ -f "$LOG" ]; then
    STARTED=$(awk -v c="$since" 'substr($0,1,10)>=c && /capture start:/' "$LOG" | wc -l | tr -d ' ')
    FAILED=$(awk  -v c="$since" 'substr($0,1,10)>=c && /headless failed/' "$LOG" | wc -l | tr -d ' ')
    NOCHANGE=$(awk -v c="$since" 'substr($0,1,10)>=c && /[0-9]Z no changes/' "$LOG" | wc -l | tr -d ' ')
    SKIP=$(awk  -v c="$since" 'substr($0,1,10)>=c && /capture skip/' "$LOG" | wc -l | tr -d ' ')
    PUSHES=$(awk -v c="$since" 'substr($0,1,10)>=c && /[0-9]Z pushed/' "$LOG" | wc -l | tr -d ' ')
  fi
  # Lint fail count from a FRESH run. It used to be read from the last "leyden lint:" line in
  # capture.log, which only the push cycle writes: between pushes (or when a push never ran) the
  # report said "lint: 0 failures" over a KB with 11 FAILs. Unparseable output counts as 1 failure.
  LINTFAIL=$("$PY" "$LDN_TOOLS/leyden-lint.py" 2>/dev/null | grep "leyden lint:" | tail -1 \
    | grep -oE '[0-9]+ fail' | grep -oE '^[0-9]+' || true)
  LINTFAIL="${LINTFAIL:-1}"
  # "Wrote" = captures that actually landed a commit. It used to be STARTED - FAILED - NOCHANGE,
  # which counted every requeued attempt (worker died, lock busy, retried later) as a write:
  # 3 "wrote" against 2 commits on a fresh install.
  WROTE=0
  [ -f "$LOG" ] && WROTE=$(awk -v c="$since" 'substr($0,1,10)>=c && /committed: capture:/' "$LOG" | wc -l | tr -d ' ')
  REQUEUED=0
  [ -f "$LOG" ] && REQUEUED=$(awk -v c="$since" 'substr($0,1,10)>=c && /capture requeue|capture ABANDONED/' "$LOG" | wc -l | tr -d ' ')
  COMMITS=$(git -C "$KB" log --oneline --since="$since" --grep="capture: auto-log" 2>/dev/null | wc -l | tr -d ' ')
  OFFSETN=$( [ -f "$OFFSETS" ] && wc -l < "$OFFSETS" | tr -d ' ' || echo 0 )
}

# ---- live effectiveness: reconcile gap (entities touched recently but not logged) ----
reconcile_gap() {
  local out; out="$("$LDN_TOOLS/leyden-reconcile.sh" "${1:-7}" 2>/dev/null)"
  printf '%s' "$out" | awk '/GAP/{f=1;next} f&&/^    - /{sub(/^    - /,"");print}'
}

# ---- capture-behind: ENDED transcripts (idle >6h) whose size exceeds the committed offset.
#      The >6h floor excludes still-active sessions: capture only runs at SessionEnd, so a growing
#      live transcript legitimately has an unprocessed tail and must NOT count as "behind".
behind_count() {
  # Claude keys its transcript dir on the launch directory; leyden-reconcile.sh owns the lookup
  # (Claude Code's exact path encoding, plus a fallback scan on each transcript's `cwd`), so the
  # two never disagree. It used to map only '/' and '.' here, which missed any workspace path
  # with a space or other symbol in it and reported "caught up" forever.
  local projs=() d
  if [ -n "${LEYDEN_PROJDIR:-}" ]; then
    projs=("$LEYDEN_PROJDIR")
  else
    while IFS= read -r d; do [ -n "$d" ] && projs+=("$d"); done \
      < <("$LDN_TOOLS/leyden-reconcile.sh" --projdirs 2>/dev/null)
  fi
  [ "${#projs[@]}" -gt 0 ] || { echo 0; return; }
  local n=0
  while IFS= read -r t; do
    [ -f "$t" ] || continue
    local sz off; sz=$(wc -c < "$t" | tr -d ' ')
    off=$(awk -F'\t' -v x="$t" '$1==x{print $2}' "$OFFSETS" 2>/dev/null); off=${off:-0}
    [ "$sz" -gt "$off" ] && n=$((n+1))
  done < <(find "${projs[@]}" -name '*.jsonl' -mmin +360 -mmin -4320 2>/dev/null)
  echo "$n"
}

# ======================= --daily : quiet one-line append =======================
if [ "${1:-}" = "--daily" ]; then
  mkdir -p "$(dirname "$HEALTH")"
  today="$(date -u +%Y-%m-%d)"
  # throttle: once per day
  [ -f "$HEALTH" ] && grep -q "^$today " "$HEALTH" && exit 0
  compute "$(cutoff_date 1)"
  gap=$(reconcile_gap 2 | grep -c . )
  behind=$(behind_count)
  printf '%s  started=%s wrote=%s nochange=%s skip=%s failed=%s gap=%s behind=%s lint_fail=%s\n' \
    "$today" "$STARTED" "$WROTE" "$NOCHANGE" "$SKIP" "$FAILED" "$gap" "$behind" "$LINTFAIL" >> "$HEALTH"
  exit 0
fi

# ======================= --report (default) : full digest =======================
DAYS=14
case "${1:-}" in
  -h|--help) echo "usage: leyden metrics [DAYS]   capture health and cost over the last DAYS days (default 14)"; exit 0 ;;
  --report) DAYS="${2:-14}" ;;
  "") ;;
  *) DAYS="$1" ;;
esac
case "$DAYS" in ""|*[!0-9]*) echo "leyden metrics: DAYS must be a whole number, got '$DAYS'" >&2; exit 2 ;; esac
SINCE="$(cutoff_date "$DAYS")"
compute "$SINCE"

echo "════════════════════════════════════════════════════════════════"
echo " leyden health report: last ${DAYS}d (since ${SINCE})   $(now)"
echo "════════════════════════════════════════════════════════════════"
echo
echo "THROUGHPUT"
echo "  sessions captured (started) : $STARTED"
echo "    ├─ wrote KB entries       : $WROTE   (git auto-log commits: $COMMITS)"
echo "    ├─ ran, nothing to log    : $NOCHANGE"
echo "    ├─ retried / given up     : $REQUEUED"
echo "    └─ headless FAILED        : $FAILED"
echo "  pre-filtered out (skips)    : $SKIP"
echo "  pushes to remote            : $PUSHES"
echo "  transcripts tracked (offset): $OFFSETN"
echo

echo "COST (headless Claude runs; capture and sweep spend tokens on your account)"
if [ -s "$COST" ]; then
  for win in 7 "$DAYS"; do
    c="$(cutoff_date "$win")"
    awk -F'\t' -v c="$c" -v w="$win" '
      substr($1,1,10)>=c {
        n++; usd=0; tok=0
        for (i=2; i<=NF; i++) {
          if ($i ~ /^cost_usd=/) usd=substr($i,10)+0
          else if ($i ~ /^(in|out)=/) { split($i, kv, "="); tok+=kv[2]+0 }
        }
        tot+=usd; tk+=tok; k=($2==""?"run":$2); by[k]+=usd; cnt[k]++
      }
      END {
        if (!n) { printf "  last %sd: no runs logged\n", w; exit }
        printf "  last %sd: $%.2f over %d run(s), %d tokens in+out", w, tot, n, tk
        for (k in by) printf "  [%s: $%.2f, %d]", k, by[k], cnt[k]
        printf "\n"
      }' "$COST"
  done
else
  echo "  no cost.log yet ($COST): runs log their cost there once the capture/sweep jobs record it"
fi
echo

echo "EFFECTIVENESS (miss detection)"
GAP="$(reconcile_gap 7)"; GAPN=$(printf '%s' "$GAP" | grep -c .)
if [ "$GAPN" -eq 0 ]; then
  echo "  ✓ every $ENT touched in the last 7d has a recent log entry"
else
  echo "  ⚠ $GAPN $ENT(s) touched but NOT logged (possible misses):"
  printf '%s\n' "$GAP" | sed 's/^/      - /'
  echo "    note: this can over-report: the CURRENT session's own transcript counts as 'touched',"
  echo "    and log edits not yet git-committed count as 'not logged'. Confirm against the actual log."
fi
echo

echo "HEALTH CHECKS"
RC=0
chk() { # <level FAIL|WARN|OK> <msg>. Plain function, no &&/|| callers (that double-fires)
  case "$1" in
    FAIL) echo "  ✗ FAIL  $2"; RC=4 ;;
    WARN) echo "  ! WARN  $2"; [ "$RC" -lt 3 ] && RC=3 ;;
    OK)   echo "  ✓ OK    $2" ;;
  esac
}
if [ "$LINTFAIL" -gt 0 ]; then chk FAIL "lint has $LINTFAIL failing file(s): run: leyden lint"; else chk OK "lint: 0 failures"; fi
if [ "$FAILED" -gt 0 ];   then chk WARN "$FAILED headless capture failure(s) in window (check '$LOG' for 529/errors)"; else chk OK "no headless failures"; fi
if [ "$GAPN" -gt 0 ];     then chk WARN "$GAPN unlogged touched-$ENT(s), see EFFECTIVENESS above"; else chk OK "no capture gaps"; fi
BEHIND=$(behind_count)
if [ "$BEHIND" -gt 0 ];   then chk WARN "$BEHIND ended transcript(s) have an unprocessed tail (capture behind?)"; else chk OK "capture caught up"; fi
# queue stuck: queue non-empty AND a stale lock is held
if [ -s "$QUEUE" ] && [ -d "$LOCK" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
  chk FAIL "queue non-empty with a >30min stale lock: drain wedged (load.sh should self-heal next start)"
else
  chk OK "queue/drain healthy"
fi
STALEREAP=$(awk -v c="$SINCE" 'substr($0,1,10)>=c && /stale lock/' "$LOG" 2>/dev/null | wc -l | tr -d ' ')
if [ "${STALEREAP:-0}" -gt 0 ]; then chk WARN "$STALEREAP stale-lock reap(s): a drain died mid-run at least once"; fi
echo
echo "TREND (.leyden/metrics/health.log, one line/day)"
if [ -f "$HEALTH" ]; then tail -n "$DAYS" "$HEALTH" | sed 's/^/  /'; else echo "  (no daily records yet; accrues once --daily runs from the SessionStart hook)"; fi
echo
echo "verdict: $( [ "$RC" -eq 0 ] && echo ALL-PASS || { [ "$RC" -eq 4 ] && echo HAS-FAILURES || echo HAS-WARNINGS; } )"
exit "$RC"
