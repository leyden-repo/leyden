#!/usr/bin/env bash
# leyden share: list resolved cases not yet shared with the team repo.
#
#   leyden share        resolved cases with no `shared:` line yet, newest first
#
# Sharing itself is done by Claude in the session, the same way any team-repo change is made: ask
# "push the <customer> <case> to the team repo". Claude follows the team repo's own template or
# existing content, works on a new branch, shows the diff, asks you once, pushes and opens a pull
# request, then adds `shared: <pull request url>` to your case. The rules are in the Leyden block of
# the workspace CLAUDE.md (templates/claude-md-team.md), written by `leyden init` / `leyden team join`.
set -uo pipefail
[ -n "${LEYDEN_CAPTURE:-}" ] && exit 0
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kb.sh"; [ "$LDN_KB_OK" = 1 ] || exit 0
PY="${LEYDEN_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"
exec "$PY" -I -B - "$KB" "$LDN_TOOLS" <<'PY'
import re, sys
from pathlib import Path
kb, tools = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(tools))
import _config as C
team = str(C.get("team.shared_dir", "") or "").strip()
T, D = C.taxonomy(), C.dirs()
case_dir = D["case"]
rows, resolved = [], 0
for f in kb.rglob(f"{case_dir}/*.md"):
    if any(p.startswith(".") for p in f.relative_to(kb).parts) or f.name in ("README.md", "index.md"):
        continue
    text = f.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    fm = m.group(1) if m else ""
    st = re.search(r"^status:\s*([^\n#]*)", fm, re.M)
    if not st or st.group(1).strip().strip("'\"").lower() != "resolved":
        continue
    resolved += 1
    # [ \t] not \s: \s crossed the newline, so an empty `shared:` key matched the closing `---`.
    # Frontmatter or the body's own `shared: <url>` line (the share rules write it there).
    if re.search(r"^shared:[ \t]*\S", text, re.M):
        continue
    ts = re.search(r"^timestamp:\s*(\S+)", fm, re.M)
    title = re.search(r"^title:\s*(.+)$", fm, re.M)
    rows.append((ts.group(1) if ts else "", f.relative_to(kb).as_posix(),
                 (title.group(1).strip() if title else f.stem)))
rows.sort(reverse=True)
if not team:
    print(f"No team repo connected (leyden team join <url>). {len(rows)} resolved {T['case']['plural']} on file.")
    sys.exit(0)
if not resolved:
    print(f"No resolved {T['case']['plural']} yet, so nothing to share.")
    sys.exit(0)
if not rows:
    print(f"Nothing pending: every resolved {T['case']['singular']} has a shared: line.")
    sys.exit(0)
print(f"{len(rows)} resolved {T['case']['plural']} not yet shared with {team}:")
for ts, rel, title in rows[:30]:
    print(f"  {ts[:10] or '----------'}  {title}  ({rel})")
if len(rows) > 30:
    print(f"  ... {len(rows) - 30} more")
print(f"\nTo share one, ask Claude: \"push <{T['entity']['singular']}> <{T['case']['singular']}> to the team repo\".")
print("Not worth sharing? Add `shared: no` to that file and it leaves this list.")
PY
