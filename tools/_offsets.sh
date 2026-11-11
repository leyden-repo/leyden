#!/usr/bin/env bash
# leyden offset store: per-consumer commit offsets (Kafka-style consumer groups).
#
# The transcripts in ~/.claude/projects/... are the immutable, append-only log ("topic"). This file
# tracks, PER CONSUMER, how far each consumer has processed each transcript: a byte offset, in
# .leyden/offsets/<consumer>.tsv (TAB-separated: <transcript_path>\t<byte_offset>).
#
# Why per-consumer instead of the old single .captured-ledger: advancing or hand-editing ONE
# consumer's offset can no longer blind the others, and any consumer can be reset to replay the log
# from any point. With a single ledger, hand-marking it could blind the capture to a session that
# was then never re-read.
#
# Sourced by capture/replay workers and the drain. Requires $KB to be set. Bash + coreutils (python
# only as a realpath fallback).
# Concurrency: single-writer per consumer is assumed (the drain lock guarantees it). Commits are
# atomic (temp file + mv) so a crash mid-commit never corrupts the offset file.

OFFSETS_DIR="${OFFSETS_DIR:-$LDN_STATE/offsets}"

_offset_file() { printf '%s/%s.tsv' "$OFFSETS_DIR" "$1"; }

# ldn_realpath <path>: the canonical spelling of a transcript path. Offsets, the retry counter and
# the queue are all keyed on the path STRING, so the same transcript reached as a relative path, an
# absolute one, or via /tmp vs /private/tmp (macOS symlink) was three different keys and got captured
# (and paid for) three times. Prefers realpath(1), falls back to python, then to a plain absolute
# path without symlink resolution.
ldn_realpath() {
  local p="$1" r
  r="$(realpath "$p" 2>/dev/null || true)"
  if [ -z "$r" ]; then
    r="$("${LEYDEN_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}" \
      -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$p" 2>/dev/null || true)"
  fi
  if [ -z "$r" ]; then
    case "$p" in /*) r="$p" ;; *) r="$(pwd -P)/$p" ;; esac
  fi
  printf '%s' "$r"
}

# The key goes to awk through ENVIRON, not -v: -v processes backslash escapes, so a path containing
# a backslash would silently never match its own entry.

# offset_get <consumer> <transcript>  ->  committed byte offset (0 if none). Looks the transcript up
# under its canonical path AND the spelling given, so offsets recorded before canonicalisation
# still count (largest wins) instead of triggering one more paid re-capture.
offset_get() {
  local f v
  f="$(_offset_file "$1")"
  [ -f "$f" ] || { echo 0; return; }
  v="$(LDN_K1="$(ldn_realpath "$2")" LDN_K2="$2" awk -F '\t' \
    '($1==ENVIRON["LDN_K1"] || $1==ENVIRON["LDN_K2"]) && $2+0>v {v=$2+0} END{print v+0}' "$f")"
  echo "${v:-0}"
}

# offset_commit <consumer> <transcript> <offset>: atomic upsert, under the canonical path (any
# entry under the raw spelling is replaced too).
offset_commit() {
  local f tmp k
  f="$(_offset_file "$1")"
  k="$(ldn_realpath "$2")"
  mkdir -p "$OFFSETS_DIR"
  tmp="$f.tmp.$$"
  { [ -f "$f" ] && LDN_K1="$k" LDN_K2="$2" awk -F '\t' '$1!=ENVIRON["LDN_K1"] && $1!=ENVIRON["LDN_K2"]' "$f"
    printf '%s\t%s\n' "$k" "$3"; } > "$tmp"
  mv "$tmp" "$f"
}

# offset_reset <consumer> [transcript]: drop one transcript's offset, or the whole consumer.
# Used to reprocess a window of transcripts.
offset_reset() {
  local f tmp
  f="$(_offset_file "$1")"
  [ -f "$f" ] || return 0
  if [ -n "${2:-}" ]; then
    tmp="$f.tmp.$$"
    LDN_K1="$(ldn_realpath "$2")" LDN_K2="$2" awk -F '\t' '$1!=ENVIRON["LDN_K1"] && $1!=ENVIRON["LDN_K2"]' "$f" > "$tmp"
    mv "$tmp" "$f"
  else
    rm -f "$f"
  fi
}

# ── Retry / re-queue ──────────────────────────────────────────────────────────────────────────────
# Shared by the capture worker (a parse or headless failure it noticed) and the drain (a worker that
# DIED: killed, OOM, set -e abort, so it could not requeue itself). On a failure the offset must NOT
# be committed (that silently drops the session forever): the transcript goes back onto the live
# queue for the next drain, bounded by LDN_MAX_RETRIES so a permanently-bad transcript cannot loop.
LDN_QUEUE="${LDN_QUEUE:-$LDN_STATE/capture-queue.txt}"
LDN_RETRY_FILE="${LDN_RETRY_FILE:-$LDN_STATE/.capture-retries}"   # gitignored: <transcript>\t<attempts>
LDN_MAX_RETRIES="${LDN_MAX_RETRIES:-3}"
retry_get() {
  [ -f "$LDN_RETRY_FILE" ] || { echo 0; return; }
  LDN_K1="$1" awk -F '\t' '$1==ENVIRON["LDN_K1"]{v=$2} END{print v+0}' "$LDN_RETRY_FILE"
}
retry_set() {
  local tmp="$LDN_RETRY_FILE.tmp.$$"
  { [ -f "$LDN_RETRY_FILE" ] && LDN_K1="$1" awk -F '\t' '$1!=ENVIRON["LDN_K1"]' "$LDN_RETRY_FILE"
    printf '%s\t%s\n' "$1" "$2"; } > "$tmp"
  mv "$tmp" "$LDN_RETRY_FILE"
}
retry_clear() {
  [ -f "$LDN_RETRY_FILE" ] || return 0
  local tmp="$LDN_RETRY_FILE.tmp.$$"
  LDN_K1="$1" awk -F '\t' '$1!=ENVIRON["LDN_K1"]' "$LDN_RETRY_FILE" > "$tmp"
  mv "$tmp" "$LDN_RETRY_FILE"
}

# ldn_requeue <transcript> <reason> <log> [counted|uncounted] [size]: put the transcript back on the
# queue.
# `counted` (default) bumps the attempt counter and, at LDN_MAX_RETRIES, gives up loudly: the offset
# is committed at the current size so it stops, and the reconcile backstop flags the gap.
# `uncounted` is for ENVIRONMENT failures (no python interpreter, a broken tool): those say nothing
# about the transcript, so they must never use up its attempts and end in an abandon.
ldn_requeue() {
  local t="$1" reason="$2" log="$3" mode="${4:-counted}" size="${5:-}" n ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [ "$mode" = uncounted ]; then
    echo "$ts capture requeue (not counted, $reason): $t" >> "$log"
    printf '%s\n' "$t" >> "$LDN_QUEUE"
    return 0
  fi
  n="$(retry_get "$t")"; n=$((n + 1))
  if [ "$n" -ge "$LDN_MAX_RETRIES" ]; then
    [ -n "$size" ] || size="$(wc -c < "$t" 2>/dev/null | tr -d ' ')"
    echo "$ts capture ABANDONED after $n attempts ($reason): $t" >> "$log"
    [ -n "$size" ] && offset_commit capture "$t" "$size"
    retry_clear "$t"
  else
    echo "$ts capture requeue (attempt $n, $reason): $t" >> "$log"
    retry_set "$t" "$n"
    printf '%s\n' "$t" >> "$LDN_QUEUE"
  fi
}

# offset_canonicalise <consumer>: one-time re-key of an offset file written before paths were
# canonicalised, so a transcript recorded as /tmp/... or a relative path is found under its real path
# instead of being captured (and paid for) again. Largest offset wins on a collision. A marker file
# makes it run once; without python it simply does not run (offset_get still tries the raw spelling).
offset_canonicalise() {
  local f py
  f="$(_offset_file "$1")"
  { [ -f "$f" ] && [ ! -f "$f.canonical" ]; } || return 0
  py="${LEYDEN_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"
  "$py" - "$f" "$f.tmp.$$" <<'PY' 2>/dev/null && mv "$f.tmp.$$" "$f" && : > "$f.canonical"
import os, sys
src = sys.argv[1]
best, order = {}, []
with open(src, encoding="utf-8", errors="surrogateescape") as fh:
    for line in fh:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2 or not parts[0]:
            continue
        key = os.path.realpath(parts[0]) if os.path.isabs(parts[0]) else parts[0]
        try:
            val = int(parts[-1])
        except ValueError:
            continue
        if key not in best:
            order.append(key)
        best[key] = max(val, best.get(key, 0))
with open(sys.argv[2], "w", encoding="utf-8", errors="surrogateescape") as out:
    for key in order:
        out.write("%s\t%d\n" % (key, best[key]))
PY
  rm -f "$f.tmp.$$" 2>/dev/null
  return 0
}

# offset_migrate_ledger <consumer>: one-time import of the legacy ".captured-ledger" (path SIZE)
# into <consumer>.tsv (path\tSIZE) if the consumer file doesn't exist yet. Idempotent.
offset_migrate_ledger() {
  local consumer f legacy
  consumer="$1"
  f="$(_offset_file "$consumer")"
  legacy="$LDN_STATE/.captured-ledger"
  { [ -f "$f" ] || [ ! -f "$legacy" ]; } && return 0
  mkdir -p "$OFFSETS_DIR"
  # last-wins per path (a trailing bigger size supersedes), tab-separate.
  awk 'NF>=2 {last[$1]=$NF} END{for (p in last) printf "%s\t%s\n", p, last[p]}' "$legacy" > "$f.tmp.$$" \
    && mv "$f.tmp.$$" "$f"
}
