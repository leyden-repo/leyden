#!/usr/bin/env python3
"""
leyden forget: remove a customer (entity) or individual entries from the KB, for a deletion
request, a mistaken capture, or data that should never have been stored.

    leyden forget acme                                   # plan: what would go, what still mentions it
    leyden forget acme --apply                           # delete + commit (current files only)
    leyden forget customers/acme/issues/old-thing.md --apply
    leyden forget "customers/*/issues/*password*" --apply

Removing a file from the current tree does NOT remove it from git history, from your remote, from
other clones, from the team repo, or from handover exports. After --apply this prints the exact
history rewrite (git filter-repo) and the follow-ups, and runs none of them: rewriting history and
force-pushing is a decision a person makes. Any secret those files ever held must be ROTATED:
deleting it does not un-leak it.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import _config as C  # noqa: E402

PROTECTED = {".git", ".leyden", "leyden.yaml", "leyden.local.yaml", "config.yaml", ".gitignore"}


def git(*args, check=False):
    return subprocess.run(["git", "-C", str(C.KB), *args], capture_output=True, text=True, check=check)


def all_files():
    for p in C.KB.rglob("*"):
        if p.is_file() and not ({".git", ".leyden"} & set(p.relative_to(C.KB).parts)):
            yield p


def resolve_targets(target: str, D: dict) -> list:
    kb = C.KB.resolve()
    # 1. an entity slug
    ent = kb / D["entity"] / target
    if "/" not in target and ent.is_dir():
        return sorted(p for p in ent.rglob("*") if p.is_file())
    # 2. a path (file or folder) inside the KB
    p = (kb / target).resolve() if not os.path.isabs(target) else Path(target).resolve()
    if p.exists():
        if kb != p and kb not in p.parents:
            raise SystemExit(f"leyden forget: {p} is outside the KB")
        return sorted(q for q in p.rglob("*") if q.is_file()) if p.is_dir() else [p]
    # 3. a glob relative to the KB root
    if any(ch in target for ch in "*?["):
        return sorted(q for q in all_files() if fnmatch.fnmatch(q.relative_to(kb).as_posix(), target))
    return []


def lock_kb(timeout=120):
    """Same lock directory and pid convention as tools/_guard.sh (ldn_git_lock)."""
    lock = C.KB / ".leyden" / ".git-op.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(timeout):
        try:
            lock.mkdir()
            (lock / "pid").write_text(str(os.getpid()))
            return lock
        except FileExistsError:
            time.sleep(1)
    raise SystemExit("leyden forget: another leyden job holds the git lock; try again in a minute")


def main() -> int:
    C.require_kb()
    D = C.dirs()
    ap = argparse.ArgumentParser(prog="leyden forget", description=__doc__.split("\n\n")[0].strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("target", help=f"{C.taxonomy()['entity']['singular']} slug, path in the KB, or glob")
    ap.add_argument("--apply", action="store_true", help="delete and commit (default: plan only)")
    a = ap.parse_args()

    kb = C.KB.resolve()
    files = resolve_targets(a.target, D)
    files = [f for f in files if not (PROTECTED & {f.relative_to(kb).parts[0], f.name})]
    if not files:
        print(f"leyden forget: nothing in the KB matches '{a.target}'", file=sys.stderr)
        return 2
    rels = [f.relative_to(kb).as_posix() for f in files]
    print(f"leyden forget {'(APPLY)' if a.apply else '(plan only: add --apply)'}: {len(rels)} file(s)")
    for r in rels[:50]:
        print(f"  - {r}")
    if len(rels) > 50:
        print(f"  ... and {len(rels) - 50} more")

    # Who still mentions it (not edited: a person decides what to say instead).
    needles = {Path(r).stem for r in rels if Path(r).stem not in ("index", "log")}
    if "/" not in a.target and (kb / D["entity"] / a.target).is_dir():
        needles.add(a.target)
    gone = set(files)
    mentions = []
    for p in all_files():
        if p in gone or p.suffix != ".md":
            continue
        try:
            t = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(n and n in t for n in needles):
            mentions.append(p.relative_to(kb).as_posix())
    if mentions:
        print(f"\nstill mentioned in {len(mentions)} other file(s) (not edited, check them by hand):")
        for m in mentions[:30]:
            print(f"  - {m}")

    if not a.apply:
        print("\nnothing changed. Re-run with --apply to delete and commit.")
        return 0

    lock = lock_kb()
    try:
        tracked = set(git("ls-files", "-z", "--", *rels).stdout.split("\0")) - {""}
        if tracked:
            git("rm", "-q", "-r", "--", *sorted(tracked), check=True)
        for f in files:
            if f.exists():
                f.unlink()
        if tracked:
            # Only these paths: anything else the person had staged stays staged, uncommitted.
            git("commit", "-q", "-m", f"forget: remove {a.target} ({len(rels)} file(s))", "--",
                *sorted(tracked), check=True)
            print(f"\ncommitted the removal ({len(tracked)} tracked file(s)).")
        else:
            print("\nremoved (nothing was committed yet, so no history to rewrite).")
    except subprocess.CalledProcessError as e:
        print(f"leyden forget: git failed: {(e.stderr or '').strip()}", file=sys.stderr)
        return 1
    finally:
        try:
            (lock / "pid").unlink()
            lock.rmdir()
        except OSError:
            pass

    if tracked:
        paths = " ".join(f"--path {shlex.quote(r)}" for r in sorted(tracked))
        remote = git("remote", "get-url", "origin").stdout.strip()
        print(f"""
The files are gone from the current tree but still in git history and on the remote.
To erase them from history too (run by hand, after reading what it does):

  1. Install git-filter-repo:   brew install git-filter-repo
  2. Rewrite this KB's history:
       git -C {shlex.quote(str(kb))} filter-repo --force --invert-paths {paths}
  3. filter-repo removes the remote; put it back and force-push every branch:
       git -C {shlex.quote(str(kb))} remote add origin {shlex.quote(remote) if remote else '<your-remote-url>'}
       git -C {shlex.quote(str(kb))} push --force --all origin
  4. Every other clone of this KB (another laptop) must be deleted and cloned again.
  5. Team repo: if any of it was shared, remove it there with a normal pull request.
  6. Handover exports and copies you sent: ask the receiver to delete them.
  7. ROTATE any secret those files ever contained. Deleting it does not un-leak it.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
