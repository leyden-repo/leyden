#!/usr/bin/env python3
"""
leyden team mode: connect a personal KB to a shared team repo so recall searches it, and so the
session knows where and how to share.

    leyden team join <url-or-path> [--yes]      find your clone of the team repo (by remote URL,
                     [--clone-to DIR]           under the workspace) or offer to clone it, record
                                                team.shared_dir in leyden.local.yaml (relative to
                                                the KB), export the team snapshot and refresh the
                                                Leyden block of the workspace CLAUDE.md
    leyden team status                          what is connected and how fresh the team snapshot is
    leyden team leave                           disconnect (removes team.shared_dir from
                                                leyden.local.yaml, refreshes the CLAUDE.md block)
    leyden team index-export [--no-fetch]       refresh <KB>/.leyden/team-snapshot/ from the team repo's
                                                default branch (what recall searches; read-only)

The team repo needs no Leyden config. Sharing is done by Claude in the session when the user asks,
following the rules in templates/claude-md-team.md (the team repo's own template or existing
content, a new branch, one confirmation before pushing, a pull request). `leyden share`
(tools/leyden-share.sh) only lists resolved cases not yet shared.

stdlib only, python 3.9+.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config as C  # noqa: E402


# ── small helpers ───────────────────────────────────────────────────────────────────────────

def run(cmd, cwd=None, check=False, inp=None):
    return subprocess.run(cmd, cwd=cwd, input=inp, capture_output=True, text=True, check=check)


def git(repo, *args, check=False):
    """stdout of a git command in `repo`, or None when it fails."""
    r = run(["git", "-C", str(repo), *args])
    if r.returncode != 0:
        if check:
            sys.exit(f"leyden team: git {' '.join(args)} failed in {repo}:\n{r.stderr.strip()}")
        return None
    return r.stdout


# ── configuration ───────────────────────────────────────────────────────────────────────────

def shared_dir() -> Path | None:
    v = C.get("team.shared_dir", "")
    if isinstance(v, dict) or not str(v).strip():
        return None
    p = Path(str(v).strip()).expanduser()
    # stored relative to the KB (written by join) so moving or copying the workspace keeps working
    return p if p.is_absolute() else Path(os.path.normpath(str(C.KB / p)))


def has_ref(repo: Path, ref: str) -> bool:
    return git(repo, "rev-parse", "--verify", "-q", ref + "^{commit}") is not None


def rev(repo: Path, ref: str) -> str:
    return (git(repo, "rev-parse", "--verify", "-q", ref + "^{commit}") or "").strip()


def has_origin(repo: Path) -> bool:
    return git(repo, "remote", "get-url", "origin") is not None


def default_branch(repo: Path) -> str:
    """Name of the team repo's default branch: origin/HEAD when known, else main/master."""
    if has_origin(repo):
        h = (git(repo, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD") or "").strip()
        if h.startswith("origin/") and has_ref(repo, h):
            return h[len("origin/"):]
        for b in ("main", "master"):
            if has_ref(repo, f"origin/{b}"):
                return b
    for b in ("main", "master"):
        if has_ref(repo, b):
            return b
    cur = (git(repo, "symbolic-ref", "--short", "-q", "HEAD") or "").strip()
    return cur or "main"


def base_ref(repo: Path) -> str:
    b = default_branch(repo)
    for ref in (f"origin/{b}", b, "HEAD"):
        if has_ref(repo, ref):
            return ref
    return "HEAD"


# ── team snapshot (what recall searches) ────────────────────────────────────────────────────

def export_snapshot(repo: Path, ref: str) -> Path | None:
    """Copy the team repo's default branch (its markdown only) to <KB>/.leyden/team-snapshot/
    via `git archive`, so recall searches what is MERGED, not whatever the clone's working tree
    happens to hold. Skips the work when the snapshot is already at that commit.

    Markdown only: recall indexes nothing else, and a team repo's config files (YAML, .env,
    JSON) may contain credentials. They
    must not land inside the personal KB folder, even gitignored."""
    sha = rev(repo, ref)
    if not sha:
        return None
    dest = C.STATE / "team-snapshot"
    mark = dest / ".leyden-snapshot"
    try:
        # a snapshot from before the markdown-only rule still holds config files: rebuild it
        if mark.read_text(encoding="utf-8").split()[0] == sha and not any(
                f.is_file() and f.suffix.lower() != ".md" and f != mark for f in dest.rglob("*")):
            return dest
    except (OSError, IndexError):
        pass
    r = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha], capture_output=True)
    if r.returncode != 0:
        return None
    C.STATE.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="team-snapshot-", dir=str(C.STATE)))
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        for m in tf.getmembers():
            name = m.name
            if not m.isfile() or name.startswith("/") or ".." in name.split("/"):
                continue
            if not name.lower().endswith(".md"):
                continue
            out = tmp / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(tf.extractfile(m).read())
    (tmp / ".leyden-snapshot").write_text(f"{sha} {ref} {repo}\n", encoding="utf-8")
    old = dest.with_name(f"team-snapshot.old.{os.getpid()}")
    if dest.exists():
        dest.rename(old)
    tmp.rename(dest)
    shutil.rmtree(old, ignore_errors=True)
    return dest


def cmd_index_export(a) -> int:
    C.require_kb()
    repo = shared_dir()
    if repo is None:
        print("leyden team index-export: no team repo connected; nothing to export")
        return 0
    if not (repo / ".git").exists():
        print(f"leyden team index-export: team repo {repo} is missing (moved?); run: leyden team join <url>",
              file=sys.stderr)
        return 1
    if not a.no_fetch and has_origin(repo) and git(repo, "fetch", "-q", "--prune", "origin") is None:
        print("leyden team: fetch failed; exporting the last fetched state", file=sys.stderr)
    ref = base_ref(repo)
    d = export_snapshot(repo, ref)
    if d is None:
        print(f"leyden team index-export: could not read {ref} in {repo}", file=sys.stderr)
        return 1
    print(f"team snapshot: {d} at {ref} {rev(repo, ref)[:12]}")
    return 0


# ── join / leave / status ───────────────────────────────────────────────────────────────────

def norm_url(u: str) -> str:
    u = u.strip().rstrip("/")
    u = re.sub(r"\.git$", "", u)
    m = re.match(r"^[\w.-]+@([^:/]+):(.+)$", u)
    if m:
        return f"{m.group(1).lower()}/{m.group(2)}"
    m = re.match(r"^(?:https?|ssh|git)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", u)
    if m:
        return f"{m.group(1).lower()}/{m.group(2)}"
    if u.startswith("file://"):
        u = u[7:]
    p = Path(u).expanduser()
    return str(p.resolve()) if p.exists() else u


def find_clone(url: str, workspace: Path) -> Path | None:
    """A clone whose origin is `url` (or which IS `url` when it is a local path): the connected
    team repo first, then the workspace (depth 3)."""
    want = norm_url(url)
    skip = {C.TOOL_ROOT.resolve()}
    if C.KB_SOURCE != "none":
        skip.add(C.KB.resolve())

    def matches(here: Path) -> bool:
        if not (here / ".git").exists() or here.resolve() in skip:
            return False
        o = git(here, "remote", "get-url", "origin")
        return (o is not None and norm_url(o) == want) or norm_url(str(here)) == want

    sd = shared_dir()
    if sd is not None and sd.is_dir() and matches(sd):
        return sd.resolve()
    for root, dirs, _files in os.walk(workspace):
        depth = len(Path(root).relative_to(workspace).parts)
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        here = Path(root)
        if (here / ".git").exists() and here.resolve() not in skip:
            if matches(here):
                return here
            dirs[:] = []   # don't descend into a clone
            continue
        if depth >= 3:
            dirs[:] = []
    return None


def write_local_team(path: Path | None):
    """Replace the `team:` block of leyden.local.yaml: shared_dir relative to the KB, or nothing."""
    f = C.LOCAL_CONFIG
    lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
    out, skipping = [], False
    for l in lines:
        if re.match(r"^team:\s*(#.*)?$", l):
            skipping = True
            continue
        if skipping and (not l.strip() or l[:1].isspace()):
            continue
        skipping = False
        out.append(l)
    while out and not out[-1].strip():
        out.pop()
    if path is not None:
        try:
            stored = os.path.relpath(Path(path).resolve(), C.KB.resolve())
        except ValueError:
            stored = str(path)
        out += (["", ] if out else []) + ["team:", f'  shared_dir: "{stored}"']
    f.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    gi = C.KB / ".gitignore"
    if (C.KB / ".git").exists() and git(C.KB, "check-ignore", "-q", "leyden.local.yaml") is None:
        with open(gi, "a", encoding="utf-8") as fh:
            fh.write("\n# per-person overlay (identity, team connection), never committed\nleyden.local.yaml\n")


def _refresh_claude_md(workspace: Path | None) -> None:
    # Rewrites the Leyden block in <workspace>/CLAUDE.md so the session knows the team repo and how
    # to share to it (or, after leave, that there is none). Reuses init's writer; never fails the caller.
    cmd = [sys.executable, "-I", "-B", str(C.TOOLS_DIR / "leyden-init.py"), "--claude-md-only"]
    if workspace is not None:
        cmd += ["--workspace", str(workspace)]
    r = run(cmd)
    out = (r.stdout or r.stderr or "").strip()
    if out:
        print(out)


def cmd_join(a) -> int:
    C.require_kb()
    workspace = Path(a.workspace).expanduser().resolve() if a.workspace else C.TOOL_ROOT.parent
    arg = a.target
    p = Path(arg).expanduser()
    repo = None
    if p.is_dir() and (p / ".git").exists():
        repo = p.resolve()
    else:
        repo = find_clone(arg, workspace)
        if repo is None:
            name = re.sub(r"\.git$", "", arg.rstrip("/").split("/")[-1].split(":")[-1]) or "team-repo"
            dest = (Path(a.clone_to).expanduser() if a.clone_to else workspace / name).resolve()
            if "://" not in arg and not re.match(r"^[\w.-]+@[\w.-]+:", arg) and not p.exists():
                sys.exit(f"leyden team: {arg} is not a URL and no such folder exists")
            if dest.exists():
                sys.exit(f"leyden team: {dest} already exists and is not a clone of {arg}; pass --clone-to")
            print(f"Cloning {arg} into {dest} (no clone of it found in the workspace).")
            if not a.yes:
                if not sys.stdin.isatty():
                    print(f"Clone it with: git clone {arg} \"{dest}\"   (or re-run with --yes)")
                    return 1
                if input(f"Clone it to {dest}? [y/N] ").strip().lower() not in ("y", "yes"):
                    return 1
            r = run(["git", "clone", "-q", "--", arg, str(dest)])
            if r.returncode != 0:
                sys.exit(f"leyden team: clone failed:\n{r.stderr.strip()}")
            repo = dest
    if repo.resolve() == C.KB.resolve() or C.TOOL_ROOT.resolve() in (repo.resolve(), *repo.resolve().parents):
        sys.exit("leyden team: the team repo must be its own clone, not your KB or the leyden tool")
    write_local_team(repo)
    export_snapshot(repo, base_ref(repo))   # recall searches the merged team record
    _refresh_claude_md(Path(a.workspace).expanduser().resolve() if a.workspace else None)
    print(f"Joined team repo {repo} (recorded team.shared_dir in {C.LOCAL_CONFIG.name}).")
    print("To share: ask Claude to push a resolved case to the team repo. It follows the repo's "
          "own format, opens a pull request on a new branch, and asks you once before pushing.")
    return 0


def cmd_leave(a) -> int:
    C.require_kb()
    write_local_team(None)
    shutil.rmtree(C.STATE / "team-snapshot", ignore_errors=True)
    for stale in ("team-index.db", ".last-team-export"):
        try:
            (C.STATE / stale).unlink()
        except OSError:
            pass
    committed = C._load_one(C.config_path()).get("team", {})
    if isinstance(committed, dict) and committed.get("shared_dir"):
        print(f"note: {C.config_path().name} also sets team.shared_dir; remove it there to disconnect")
    _refresh_claude_md(None)
    print("Left team mode. Your KB and the team repo are unchanged.")
    return 0


def cmd_status(a) -> int:
    C.require_kb()
    repo = shared_dir()
    print(f"KB:         {C.KB}")
    if repo is None:
        print("Team repo:  not connected (leyden team join <url>)")
        return 0
    print(f"Team repo:  {repo}{'' if (repo / '.git').exists() else '  (MISSING: not a git clone)'}")
    if not (repo / ".git").exists():
        return 1
    ref = base_ref(repo)
    print(f"Remote:     {(git(repo, 'remote', 'get-url', 'origin') or 'none').strip()}")
    snap = C.STATE / "team-snapshot" / ".leyden-snapshot"
    try:
        sha = snap.read_text(encoding="utf-8").split()[0]
        print(f"Search:     team snapshot at {ref} {sha[:12]} (refreshed at session start, at most every 6 h)")
    except (OSError, IndexError):
        print("Search:     no team snapshot yet (leyden team index-export)")
    print("Sharing:    ask Claude to push a resolved case to the team repo; `leyden share` lists pending ones")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="leyden team", description="Leyden team mode")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("join", help="connect your KB to the team repo")
    p.add_argument("target", help="team repo URL or local clone path")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--workspace", help="where to look for an existing clone (default: the folder holding leyden/)")
    p.add_argument("--clone-to", help="where to clone when no clone is found")
    sub.add_parser("status")
    sub.add_parser("leave")
    p = sub.add_parser("index-export", help="refresh the team snapshot recall searches")
    p.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    return {"join": cmd_join, "status": cmd_status, "leave": cmd_leave,
            "index-export": cmd_index_export}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
