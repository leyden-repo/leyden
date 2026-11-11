#!/usr/bin/env bash
# leyden promote (v1): prepare a shared-scope file for the team repo.
#
#   tools/leyden-promote.sh <entities>/<slug>/<cases>/<file>.md
#
# Publishing is the gated act. This script does the mechanical half of the promotion:
#   1. verifies the file is in SHAREABLE scope (<entities>/<slug>/ index.md, <contexts>/, <cases>/)
#   2. copies it to .promote-staging/<same path>
#   3. rewrites private-scope links (<playbooks>/, internal/, projects/, memory/, log.md) to plain
#      text: the team repo will not have those files
#   4. runs the secret scanner and refuses to stage on any hard finding
#   5. flags lines a human MUST review (internal chat links, ticket links, candour markers) and
#      prints the sanitisation checklist
# The staged copy is then reviewed by a human and PR'd into the team repo BY HAND (v1). The
# original file is never modified. Directory names come from leyden.yaml (tools/_config.py).
#
# Optional config used in messages only:
#   promotion.playbook_target_dir   where <playbooks> go when they leave the private KB
set -uo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kb.sh"; [ "$LDN_KB_OK" = 1 ] || { ldn_no_kb_msg; exit 1; }   # KB, LDN_TOOLS, LDN_STATE (tools/_kb.sh)
PY="${LEYDEN_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"
eval "$("$PY" "$LDN_TOOLS/_config.py" shell 2>/dev/null)" || true
E="${LDN_ENTITIES:-customers}"; X="${LDN_CONTEXTS:-deployments}"; C="${LDN_CASES:-issues}"
P="${LDN_PLAYBOOKS:-runbooks}"; I="${LDN_INTERNAL:-internal}"; J="${LDN_PROJECTS:-projects}"
ENT="${LDN_ENTITY:-customer}"
PB_TARGET="$("$PY" "$LDN_TOOLS/_config.py" get promotion.playbook_target_dir 2>/dev/null || true)"
TICKET_HOST="$("$PY" "$LDN_TOOLS/_config.py" get ticketing.url_base 2>/dev/null | sed -E 's#^[a-z]+://##; s#/.*$##' || true)"

REL="${1:-}"
[ -n "$REL" ] || { echo "usage: leyden-promote.sh $E/<slug>/{index.md|$X/*.md|$C/*.md}"; exit 2; }
# Canonicalize BEFORE the scope check: shell `case` globs let `*` match `/` and `..`, so
# "<entities>/x/<cases>/../../../<playbooks>/secret.md" passed the guard and staged a PRIVATE file
# (and with enough ../ segments the staged copy escaped .promote-staging entirely).
REL="$("$PY" -c 'import os,sys; print(os.path.normpath(sys.argv[1]))' "$REL")"
case "$REL" in
  /*|../*|*/..|*/../*|..) echo "REFUSED: path escapes the KB: $REL"; exit 1 ;;
esac
SRC="$KB/$REL"
[ -f "$SRC" ] || { echo "no such file: $REL"; exit 2; }

# Quoted variables in a `case` pattern match literally; the unquoted `*` still globs.
case "$REL" in
  "$E"/*/"$C"/*.md|"$E"/*/index.md|"$E"/*/"$X"/*.md) ;;
  *) echo "REFUSED: '$REL' is not in shareable scope ($E/ index, $X, $C only)."
     echo "$P promote to ${PB_TARGET:-your public docs repo (set promotion.playbook_target_dir)} via PR; $I $C promote to your ticket tracker."; exit 1 ;;
esac

STAGE="$KB/.promote-staging/$REL"
mkdir -p "$(dirname "$STAGE")"

# 3) private-scope links -> plain text (keep the link text, drop the target).
# The exit IS checked (no set -e in this script): a failed stage used to fall through and print
# the success checklist anyway, or bless a stale staged copy from an earlier run.
if ! "$PY" - "$SRC" "$STAGE" "$P" "$I" "$J" "$E" <<'PY'
import re, sys
src, dst, playbooks, internal, projects, entities = sys.argv[1:7]
text = open(src, encoding="utf-8").read()
private_dirs = "|".join(re.escape(d) for d in (playbooks, internal, projects, "memory"))
PRIVATE = re.compile(r"\[([^\]]+)\]\(((?:\.\./)*)(?:" + private_dirs + r")/[^)]+\)")
LOG = re.compile(r"\[([^\]]+)\]\(((?:\.\./)*)(?:" + re.escape(entities) + r"/[^)]*/)?log\.md[^)]*\)")
n = len(PRIVATE.findall(text)) + len(LOG.findall(text))
text = PRIVATE.sub(r"\1", text)
text = LOG.sub(r"\1", text)
open(dst, "w", encoding="utf-8").write(text)
print(f"staged with {n} private-scope link(s) flattened to plain text")
PY
then
  echo "REFUSED: staging step failed, nothing promoted."
  rm -f "$STAGE"
  exit 1
fi

# 4) hard secret findings block the promotion outright
if ! "$PY" "$LDN_TOOLS/leyden-redact.py" --scan "$STAGE" >/dev/null; then
  echo "REFUSED: secret scanner found hard findings in the staged copy:"
  "$PY" "$LDN_TOOLS/leyden-redact.py" --scan "$STAGE" | grep -v "entropy-hex"
  rm -f "$STAGE"
  exit 1
fi

# 5) lines a human must look at before this leaves the private repo: internal chat threads
# (invisible to the team repo's readers), ticket links (check they are team-visible), candour.
echo
echo "REVIEW these lines by hand (internal links / candour markers):"
# The second arrow form is the em-dash arrow (U+2014 followed by '>'), written as an octal escape
# so this file stays plain ASCII.
EMDASH="$(printf '\342\200\224')"
REVIEW_RE="slack\.com/archives|teams\.microsoft\.com|(-> |${EMDASH}>)|\b(blame|screwed|mess|hack)\b"
[ -n "$TICKET_HOST" ] && REVIEW_RE="$REVIEW_RE|$(printf '%s' "$TICKET_HOST" | sed 's/\./\\./g')"
grep -nE "$REVIEW_RE" "$STAGE" | head -20 || echo "  (none flagged)"

cat <<CHECKLIST

Sanitisation checklist (from the promotion contract), confirm each before PR:
  [ ] no private-worklog candour (venting, guesses stated as facts, internal friction)
  [ ] no internal blame or internal-only names
  [ ] no internal chat links (thread content is invisible to the team repo's readers)
  [ ] no secrets (scanner passed, but scan the diff yourself once)
  [ ] status:, $ENT:, author: frontmatter correct
Staged copy: .promote-staging/$REL
Review it, then copy into the team repo (team.shared_dir, if set) and open a PR.
CHECKLIST
