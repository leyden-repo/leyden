#!/usr/bin/env bash
# Snapshot files that live OUTSIDE the KB (workspace notes, Claude settings, agents, skills, project
# memory) into <kb>/workspace-config/ so they get backed up + pushed alongside the KB. One-way
# mirror: the live files are the source of truth, the copy under workspace-config/ is never edited.
#
# What gets mirrored is entirely config-driven. In leyden.yaml:
#
#   workspace:
#     mirrors: ["/abs/path/to/src|dest/relative/dir", "~/.claude/skills/my-skill|_user-claude/skills/my-skill", "CLAUDE.md|CLAUDE.md"]
#
# Each item is "<src>|<dest-rel>": src is an absolute path, a ~-prefixed path, or a path relative
# to the workspace root (the KB's parent directory, or $LEYDEN_WORKSPACE); dest-rel is relative to
# workspace-config/ (defaults to the src basename when omitted). Sources may be files or
# directories; directories are mirrored with --delete. Missing sources are skipped silently.
# With no `workspace.mirrors` configured this script does nothing and says so.
set -euo pipefail
[ -n "${LEYDEN_CAPTURE:-}" ] && exit 0

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kb.sh"; [ "$LDN_KB_OK" = 1 ] || exit 0   # KB, LDN_TOOLS, LDN_STATE (tools/_kb.sh)
ROOT="${LEYDEN_WORKSPACE:-$(cd "$KB/.." && pwd)}"
DEST="$KB/workspace-config"
LOG="$LDN_STATE/capture.log"
PY="${LEYDEN_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"

# One mirror spec per line (the `get` subcommand space-joins lists, which breaks paths).
MIRRORS="$("$PY" - "$KB" <<'PY' 2>/dev/null || true
import sys, os
sys.path.insert(0, os.environ["LDN_TOOLS"])
from _config import get
v = get("workspace.mirrors", [])
if isinstance(v, str):
    v = [v] if v.strip() else []
for item in (v if isinstance(v, list) else []):
    item = str(item).strip().strip("\"'")
    if item:
        print(item)
PY
)"

if [ -z "$MIRRORS" ]; then
  echo "leyden-sync-workspace: no mirrors configured (workspace.mirrors in leyden.yaml); nothing to sync"
  exit 0
fi

command -v rsync >/dev/null 2>&1 || { echo "leyden-sync-workspace: rsync not found; skipping" >&2; exit 0; }
mkdir -p "$DEST"

n=0
while IFS= read -r spec; do
  [ -n "$spec" ] || continue
  src="${spec%%|*}"
  rel="${spec#*|}"
  [ "$rel" = "$spec" ] && rel="$(basename "$src")"   # no '|': dest = basename of src
  case "$src" in
    "~"|"~/"*) src="$HOME${src#\~}" ;;
    /*) ;;
    *) src="$ROOT/$src" ;;
  esac
  # dest must stay inside workspace-config/: refuse absolute or parent-escaping dest specs
  case "$rel" in
    /*|../*|*/../*|*/..|..) echo "leyden-sync-workspace: refusing dest outside workspace-config/: $rel" >&2; continue ;;
  esac
  [ -e "$src" ] || continue
  # Source must resolve INSIDE the workspace root or ~/.claude (project notes, Claude settings,
  # agents, skills, memory: what this mirror is for), and never into a credentials store. The
  # config can be edited by a pulled change or a hijacked capture: `workspace.mirrors: ["~/.ssh|x"]`
  # used to rsync private keys into the KB, and the next push published them.
  real="$(cd "$(dirname "$src")" 2>/dev/null && printf '%s/%s' "$(pwd -P)" "$(basename "$src")")" || continue
  [ -d "$src" ] && real="$(cd "$src" 2>/dev/null && pwd -P)"
  rroot="$(cd "$ROOT" 2>/dev/null && pwd -P)"; rclaude="$(cd "$HOME/.claude" 2>/dev/null && pwd -P || echo "$HOME/.claude")"
  case "$real/" in
    "$rroot"/*|"$rclaude"/*) ;;
    *) echo "leyden-sync-workspace: refusing source outside the workspace and ~/.claude: $src" >&2; continue ;;
  esac
  case "$real/" in
    "$KB"/*) echo "leyden-sync-workspace: refusing source inside the KB itself: $src" >&2; continue ;;
  esac
  case "/$real/" in
    */.ssh/*|*/.aws/*|*/.config/*|*/.gnupg/*|*/.kube/*|*/.azure/*|*/.docker/*|*/Keychains/*|*/.password-store/*|\
    */.netrc/*|*/.git-credentials/*|*/.npmrc/*|*/.pypirc/*|*/.env/*|*/.env.*/*|*/.credentials*|*/credentials.json/*)
      echo "leyden-sync-workspace: refusing a credentials location as a mirror source: $src" >&2; continue ;;
  esac
  if [ -d "$src" ]; then
    mkdir -p "$DEST/$rel"
    rsync -a --delete --exclude '.credentials*' --exclude '.env' --exclude '.env.*' --exclude '*.pem' --exclude '*.key' "$src/" "$DEST/$rel/"
  else
    mkdir -p "$DEST/$(dirname "$rel")"
    rsync -a "$src" "$DEST/$rel"
  fi
  n=$((n + 1))
done <<EOF
$MIRRORS
EOF

echo "$(date -u +%FT%TZ) synced workspace-config ($n mirror(s))" >> "$LOG"
