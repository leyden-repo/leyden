#!/usr/bin/env python3
"""
leyden retrieval index: SQLite FTS5 (BM25) over the KB, plus zoxide-style frecency aging.

The markdown files stay the single source of truth. The index is THROWAWAY: gitignored,
rebuilt from the files in one pass (hundreds of files, milliseconds), never merged, never migrated.
If it is ever wrong or corrupt, delete it: the search hook falls back to its grep scan and
the next rebuild recreates it. Logic borrowed from zk (disposable FTS5 index over markdown)
and zoxide (frecency + aging).

The index is vocabulary-agnostic by construction: it indexes every markdown file under the KB
regardless of which slot directory it lives in, so nothing here depends on leyden.yaml except
the optional `workspace.search_allow` list and `retrieval.max_rank`.

Usage:
    python3 tools/leyden-index.py                 # (re)build <KB>/.leyden/index.db
    python3 tools/leyden-index.py --team          # (re)build <KB>/.leyden/team-index.db over the
                                                  # team repo clone (team.shared_dir), read-only
    python3 tools/leyden-index.py --age           # zoxide aging pass over rank:/last_used:
    python3 tools/leyden-index.py --age --dry-run # show what aging would change, write nothing

Frecency contract (frontmatter, maintained by the capture worker per capture-prompt.md):
    rank: <float>          # +1 every time the entry's fix is actually APPLIED in a session
    last_used: YYYY-MM-DD  # stamped on the same event
The search hook multiplies BM25 relevance by a recency-bucketed rank, so credible entries
flow upward without ever displacing a better lexical match.

Aging (zoxide's algorithm, adapted): when the sum of all ranks exceeds LEYDEN_MAX_RANK
(default 500), every rank is scaled so the total returns to 90% of the cap, and entries
falling below 1.0 lose their rank:/last_used: keys entirely (the file stays; only the boost
dies). Old popularity fades unless the entry keeps earning use. Run by the batched push job
BEFORE the index rebuild, so the committed frontmatter and the index agree.

stdlib only. Needs an SQLite build with FTS5 (the macOS system python has it).
"""

import hashlib
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import get as _cfg_get  # noqa: E402

import _config as _cfgmod  # noqa: E402

KB = _cfgmod.KB   # resolved KB (tools/_config.py: LEYDEN_HOME, .kb-path, legacy)
DB = _cfgmod.STATE / "index.db"   # machine-local, in the KB's gitignored .leyden/

# Same knowledge scope as the search hook's grep fallback, plus internal/ (the grep scan
# predates internal/ and never covered it; the index should not inherit that gap).
SKIP_TOP = {"tools", "_templates", "templates", "team-template", "memory", ".git",
            ".promote-staging"}
# Scratch space is never knowledge, at any depth: a workspace `scratch/` or `tmp/`
# that ends up under the KB holds drafts, dumps and copies of other entries.
SKIP_ANY = {"scratch", "tmp", ".git", ".leyden"}   # .leyden: runtime state
# workspace-config/ is skipped except for files listed in leyden.yaml under
# `workspace.search_allow: [workspace-config/some-note.md]` (KB-relative paths).
# This MUST be the same key the search hook reads (leyden-search.sh: workspace.search_allow).
# It used to read `retrieval.workspace_allow` here and nowhere else, so a file allowlisted the
# documented way was never indexed: the FTS engine could not return it at all and only the grep
# fallback ever saw it. `retrieval.workspace_allow` is still accepted as a legacy alias.
def _allow_list(*paths):
    out = set()
    for p in paths:
        v = _cfg_get(p, [])
        if isinstance(v, str):
            v = [v] if v.strip() else []
        if isinstance(v, list):
            out |= {str(x).strip().strip("\"'") for x in v if str(x).strip()}
    return out


WS_ALLOW = _allow_list("workspace.search_allow", "retrieval.workspace_allow")

MAX_RANK_DEFAULT = 500.0


def split_fm(raw: str):
    """(fm_dict, body): flat `key: value` frontmatter, no YAML dependency (same as leyden-issues.py)."""
    if not raw.startswith("---"):
        return {}, raw
    lines = raw.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, raw
    fm = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        if k != k.strip():
            continue
        fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, "\n".join(lines[end + 1:])


# Markdown at the KB ROOT that is KB content. Everything else up there (README, CONVENTIONS,
# ARCHITECTURE, SETUP, a LICENSE or CHANGELOG) is repo documentation about the tooling, and
# indexing it means the install instructions compete with real entries for every query: a trial
# KB with five seeded entries returned the tool repo's own README as a top-5 hit. Same rule as
# leyden-lint.py's ROOT_KB_FILES and leyden-search.sh's excluded().
ROOT_KB_FILES = {"index.md", "open.md", "closed.md", "log.md"}


def _team_snapshot():
    """(<KB>/.leyden/team-snapshot, recorded repo path) when `leyden team index-export` wrote one."""
    snap = _cfgmod.STATE / "team-snapshot"
    try:
        parts = (snap / ".leyden-snapshot").read_text(encoding="utf-8").strip().split(" ", 2)
    except Exception:
        return None, ""
    return snap, (parts[2] if len(parts) == 3 else "")


def team_root():
    """What recall searches for the team repo, or None. Prefers the snapshot of the team repo's
    MERGED default branch (refreshed by join/index-export), because the clone's working tree
    is never fast-forwarded by Leyden and goes stale; falls back to the clone itself."""
    v = _cfg_get("team.shared_dir", "")
    if isinstance(v, dict) or not str(v).strip():
        return None
    snap, _ = _team_snapshot()
    if snap is not None and snap.is_dir():
        return snap
    p = Path(str(v).strip()).expanduser()
    if not p.is_absolute():
        p = KB / p   # team join stores it relative to the KB
    return p if p.is_dir() else None


def team_label():
    """`team:<repo name>` for hits from the team repo, named after the real repo, not the snapshot."""
    _, repo = _team_snapshot()
    v = _cfg_get("team.shared_dir", "")
    name = repo or ("" if isinstance(v, dict) else str(v).strip())
    return "team:" + os.path.basename(os.path.normpath(name or "team"))


def kb_files(root=None, team=False):
    # os.walk with pruning instead of KB.rglob: rglob walked .git/ and every skipped tree just to
    # throw the results away, and the search hook now calls this on every prompt (signature()).
    # team=True walks the team repo instead: every entity folder's markdown (READMEs,
    # records, case files), never root files, _templates/, .github/ or ci/.
    root = Path(root) if root is not None else KB
    found = []
    for here, dirs, files in os.walk(root):
        rel_root = os.path.relpath(here, root)
        rel_root = "" if rel_root == "." else rel_root.replace(os.sep, "/")
        if team:
            dirs[:] = [d for d in dirs if d not in SKIP_ANY and not d.startswith((".", "_"))
                       and not (not rel_root and d in ("ci", "tools"))]
        else:
            dirs[:] = [d for d in dirs if d not in SKIP_ANY and not (not rel_root and d in SKIP_TOP)]
        for name in files:
            if not name.endswith(".md"):
                continue
            rel = f"{rel_root}/{name}" if rel_root else name
            if team:
                if not rel_root:
                    continue  # root-level repo docs
                found.append((root / rel, rel))
                continue
            if not rel_root:
                continue      # root files: repo docs, or catalogs/dashboards (index/open/closed/log.md)
                              # that match every query and displace the entries they point to
            if rel.split("/", 1)[0] == "workspace-config" and rel not in WS_ALLOW:
                continue
            found.append((root / rel, rel))
    for f, rel in sorted(found, key=lambda x: x[1]):
        yield f, rel


def signature(root=None, team=False) -> str:
    """Fingerprint of the indexable file SET and its contents' stat: count + hash over
    (path, mtime_ns, size). The search hook compares it with the one stored at build time.
    It used to compare only the newest mtime against the index's, so a DELETED or RENAMED entry
    (which leaves no newer file behind) stayed searchable until something else changed."""
    h = hashlib.sha1(b"schema-2\n")   # bump when the kb table changes, so old index.db files rebuild
    n = 0
    for f, rel in kb_files(root, team):
        try:
            st = f.stat()
        except OSError:
            continue
        h.update(f"{rel}\0{st.st_mtime_ns}\0{st.st_size}\n".encode("utf-8", "replace"))
        n += 1
    return f"{n}:{h.hexdigest()}"


def as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def listish(v: str) -> str:
    """'[a, b c]' -> 'a b c': flatten frontmatter lists into indexable text."""
    return " ".join(s.strip() for s in (v or "").strip("[]").split(",") if s.strip())


# ── build ────────────────────────────────────────────────────────────────────────────────

def build(root=None, db=None, team=False) -> int:
    # Per-pid temp: two sessions can prompt at once and both trigger a lazy rebuild; a shared
    # temp path would have them writing into the same half-built db. os.replace stays atomic,
    # so concurrent builds just harmlessly replace each other with equally-fresh indexes.
    db = Path(db) if db is not None else DB
    tmp = db.with_suffix(f".db.tmp{os.getpid()}")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    # porter stemming: "freezing" finds "freeze", recall the grep scan never had.
    con.execute(
        "CREATE VIRTUAL TABLE kb USING fts5("
        "path UNINDEXED, title, description, tags, symptoms, body, pathtext, "
        "tokenize='porter unicode61')")
    con.execute("CREATE TABLE meta (path TEXT PRIMARY KEY, title TEXT, rank REAL, last_used TEXT)")
    con.execute("CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT)")
    # Taken BEFORE reading the files: an edit landing mid-build leaves a mismatch, so the next
    # search rebuilds again rather than trusting an index that missed the edit.
    con.execute("INSERT INTO info VALUES ('signature', ?)", (signature(root, team),))
    n = 0
    for f, rel in kb_files(root, team):
        try:
            raw = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm, body = split_fm(raw)
        m = re.search(r"(?m)^#\s+(.+)$", body)
        title = fm.get("title") or (m.group(1).strip() if m else f.stem)
        con.execute(
            "INSERT INTO kb VALUES (?,?,?,?,?,?,?)",
            # Path words in their own column (still matchable), so body snippets start with body text.
            (rel, title, fm.get("description", ""), listish(fm.get("tags", "")),
             listish(fm.get("symptoms", "")), body, rel))
        con.execute(
            "INSERT INTO meta VALUES (?,?,?,?)",
            (rel, title, as_float(fm.get("rank")), fm.get("last_used", "")))
        n += 1
    con.commit()
    con.close()
    try:
        os.replace(tmp, db)  # atomic: a concurrent hook read never sees a half-built index
    finally:
        tmp.unlink(missing_ok=True)
    print(f"indexed {n} files -> {db.relative_to(KB) if db.is_relative_to(KB) else db}"
          if hasattr(db, "is_relative_to") else f"indexed {n} files -> {db}")
    return 0


# ── aging (zoxide) ───────────────────────────────────────────────────────────────────────

RANK_RE = re.compile(r"(?m)^rank:\s*([0-9.]+)\s*$")
RANK_LINE_RE = re.compile(r"(?m)^rank:\s*[0-9.]+\s*\n")   # includes the newline, for clean removal
LAST_USED_RE = re.compile(r"(?m)^last_used:\s*\S+\s*\n")


def age(dry_run: bool) -> int:
    cfg_cap = as_float(_cfg_get("retrieval.max_rank"), MAX_RANK_DEFAULT)
    cap = as_float(os.environ.get("LEYDEN_MAX_RANK"), cfg_cap)
    ranked = []
    for f, rel in kb_files():
        raw = f.read_text(encoding="utf-8", errors="ignore")
        m = RANK_RE.search(raw)
        if m:
            ranked.append((f, rel, raw, float(m.group(1))))
    total = sum(r for _, _, _, r in ranked)
    if total <= cap:
        print(f"aging: total rank {total:g} <= cap {cap:g}, nothing to do")
        return 0
    k = (0.9 * cap) / total
    for f, rel, raw, r in ranked:
        new = r * k
        if new < 1.0:
            out = RANK_LINE_RE.sub("", raw)
            out = LAST_USED_RE.sub("", out)
            action = f"{rel}: rank {r:g} -> dropped"
        else:
            out = RANK_RE.sub(f"rank: {new:.2f}", raw)
            action = f"{rel}: rank {r:g} -> {new:.2f}"
        print(("would " if dry_run else "") + action)
        if not dry_run and out != raw:
            f.write_text(out, encoding="utf-8")
    print(f"aging: total {total:g} -> ~{0.9 * cap:g} (cap {cap:g}, k={k:.3f})")
    return 0


def main():
    _cfgmod.require_kb()
    args = sys.argv[1:]
    if "--age" in args:
        return age(dry_run="--dry-run" in args)
    if "--team" in args:
        root = team_root()
        if root is None:
            print("no team repo connected (team.shared_dir); nothing to index")
            return 0
        return build(root, _cfgmod.STATE / "team-index.db", team=True)
    return build()


if __name__ == "__main__":
    sys.exit(main())
