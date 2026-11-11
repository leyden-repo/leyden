#!/usr/bin/env python3
"""
leyden lint: conformance + link health + dashboard sync + staleness.

Checks a KB against leyden's schema (CONVENTIONS.md). The vocabulary (entity /
context / case / playbook words, directory names, frontmatter keys) comes from leyden.yaml via
tools/_config.py, so the same checks run on accounts/environments/tickets/how-tos and on
customers/sites/issues/runbooks alike.

  FAIL   no frontmatter, missing/unknown `type`, case with bad `status`,
         dangling relative md link inside the KB
  WARN   missing description/timestamp, dangling frontmatter relation
         (entity/context/playbook slug), relative link leaving the KB that
         doesn't exist on disk, dashboard out of sync with case statuses
  STALE  git last-commit newer than frontmatter `timestamp` by >2 days
         (content moved, timestamp didn't), open/blocked case untouched >14 days
         (needs a status pass)

Exit 1 on any FAIL (or on STALE too with --stale-fail). stdlib only.

Usage:
    python3 tools/leyden-lint.py [--stale-fail]
    python3 tools/leyden-lint.py --fix           # auto-repair the mechanical failures first, then report
    python3 tools/leyden-lint.py --stale [DAYS]  # ONLY the stale-open-case report (default 14 days),
                                                 # one line per case, always exit 0 (`leyden leak`)

--fix repairs only the deterministic, no-judgment problems: un-escapes literal-\\n
frontmatter, stubs missing frontmatter (type inferred from path), fills missing
`timestamp`/`description`, and bumps a `timestamp` that a later commit moved past.
It never touches case *statuses* or the "needs a status pass" staleness, those need
a human decision, so those still surface in the report after a --fix run.
"""

import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import SLOTS, dirs, get as _cfg_get, taxonomy  # noqa: E402


def _cfg_list(path):
    """Config value as a list of non-empty strings; [] when absent or malformed."""
    v = _cfg_get(path, [])
    if isinstance(v, str):
        v = [v] if v.strip() else []
    return [str(x).strip().strip("\"'") for x in v if str(x).strip()] if isinstance(v, list) else []

import _config as _cfgmod  # noqa: E402

KB = _cfgmod.KB   # resolved KB (tools/_config.py: LEYDEN_HOME, .kb-path, legacy)
# `memory/` holds auto-memory files on a DIFFERENT schema (type: feedback|user|reference):
# linting them against the KB type enum is a false FAIL, so skip the dir entirely.
# `team-template/` is the seed for a shared team repo that ships with the tooling, not KB
# content: its README has no frontmatter and never will, so linting it is a permanent FAIL.
SKIP_DIRS = {"workspace-config", "tools", ".leyden", "_templates", "templates", "team-template", "memory",
             ".git", ".promote-staging"}
# Root-level markdown that is REPO documentation, not KB content. Only the KB's own root files
# carry frontmatter; anything else at the root (SETUP.md, ARCHITECTURE.md, CONTRIBUTING.md, a
# LICENSE or CHANGELOG) is a doc someone wrote for humans and will never have a `type:`. The list
# used to be the two literal names {README.md, CONVENTIONS.md}, so the day a KB gained a third
# root doc the lint acquired a FAIL that no edit short of deleting the file could clear: a KB with
# a SETUP.md carried that FAIL on every run, which also pinned the `lint_fail`
# health metric at 1 and made a real regression indistinguishable from the standing noise.
ROOT_KB_FILES = {"index.md", "open.md", "closed.md", "log.md"}
# extra paths (KB-relative) to skip, for anything the rule above cannot know about
SKIP_FILES = set(_cfg_list("lint.skip_files"))
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# A template placeholder left in place: `<slug>`, `<TICKET-XXX>`, `[<x-slug>]`. The redactor's
# `<REDACTED:...>` markers are real content, not placeholders.
PLACEHOLDER_RE = re.compile(r"<(?!REDACTED:)[^<>\s][^<>]*>")


def read_md(f, bad=None):
    """File text; never raises on a non-UTF-8 byte. One stray byte used to crash the whole lint
    (and the push cycle then silently skipped the lint and the dashboards). Decoded with
    replacement characters instead, and the file is recorded in `bad` so it gets reported."""
    data = f.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if bad is not None:
            bad.add(f)
        return data.decode("utf-8", errors="replace")

# ── vocabulary (from leyden.yaml) ─────────────────────────────────────────────
T = taxonomy()
D = dirs()
ENTITY, ENTITIES = T["entity"]["singular"], T["entity"]["plural"]
CONTEXT, CONTEXTS = T["context"]["singular"], T["context"]["plural"]
CASE, CASES = T["case"]["singular"], T["case"]["plural"]
PLAYBOOK, PLAYBOOKS = T["playbook"]["singular"], T["playbook"]["plural"]
FIXED_TYPES = {"index", "log", "project"}
TYPES = FIXED_TYPES | {T[s]["singular"] for s in SLOTS}

# Canonical case statuses. `open-diagnosed` (root-caused, fix not yet applied) and
# `in-progress` (actively being fixed) were added because the capture pass + real worklogs
# kept reaching for them: they're genuine states between `open` and `resolved`.
# `discarded` (dropped on purpose: not-a-bug, superseded, won't-fix, ask went away) is a terminal
# state distinct from `resolved`, and the decision is worth keeping so it isn't re-litigated.
ISSUE_STATUSES = {"open", "open-diagnosed", "in-progress", "blocked", "workaround", "review",
                  "resolved", "discarded"}
OPEN_STATUSES = {"open", "open-diagnosed", "in-progress", "blocked", "workaround", "review"}
TERMINAL_STATUSES = {"resolved", "discarded"}
TIMESTAMP_SLACK = timedelta(days=2)     # git-newer-than-timestamp tolerance
ISSUE_STALE_AFTER = timedelta(days=14)  # open case untouched this long -> status pass


def split_fm(raw: str):
    if not raw.startswith("---"):
        return None, raw
    lines = raw.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None, raw
    fm = {}
    for ln in lines[1:end]:
        if ":" in ln and not ln.lstrip().startswith("#"):
            k, _, v = ln.partition(":")
            fm[k.strip()] = v.split("#")[0].strip().strip('"')
    return fm, "\n".join(lines[end + 1:])


def parse_iso(s: str):
    if not s:
        return None
    s = s.strip().strip('"').replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def git_last_commit(path: Path):
    """ISO datetime of the last commit touching path, or None (untracked / no git)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(KB), "log", "-1", "--format=%cI", "--", str(path.relative_to(KB))],
            capture_output=True, text=True, timeout=10)
        return parse_iso(r.stdout.strip()) if r.stdout.strip() else None
    except Exception:
        return None


def last_content_commit(path: Path):
    """Like git_last_commit, but skips commits that only changed this file's `timestamp:` line.
    A restamp is itself a commit, so `lint --fix` (or a sweep, or a hand restamp) used to leave
    every file it fixed flagged STALE again: the restamp commit is newer than the date it wrote.
    Only called for files the cheap check already flagged."""
    try:
        r = subprocess.run(["git", "-C", str(KB), "log", "-8", "--format=%H%x09%cI", "--",
                            str(path.relative_to(KB))], capture_output=True, text=True, timeout=10)
        for row in r.stdout.strip().split("\n"):
            if "\t" not in row:
                continue
            h, when = row.split("\t", 1)
            d = subprocess.run(["git", "-C", str(KB), "show", "--format=", "-U0", h, "--",
                                str(path.relative_to(KB))], capture_output=True, text=True, timeout=10)
            changed = [ln for ln in d.stdout.split("\n")
                       if ln[:1] in ("+", "-") and not ln.startswith(("+++", "---"))]
            if changed and all(re.match(r"^[+-]timestamp:", ln) for ln in changed):
                continue
            return parse_iso(when)
    except Exception:
        return None
    return None


def kb_files():
    for f in sorted(KB.rglob("*.md")):
        rel = f.relative_to(KB)
        if rel.parts[0] in SKIP_DIRS:
            continue
        if len(rel.parts) == 1 and f.name not in ROOT_KB_FILES:
            continue          # repo documentation at the KB root
        if f.name in SKIP_FILES or rel.as_posix() in SKIP_FILES:
            continue
        yield f, rel


def _listish(v: str):
    """'[a, b]' -> ['a', 'b'] for the inline-list frontmatter values."""
    return [s.strip() for s in (v or "").strip("[]").split(",") if s.strip()]


# ── --fix helpers ────────────────────────────────────────────────────────────
def _iso_date(dt):
    return dt.strftime("%Y-%m-%dT00:00:00Z")


def _commit_or_today(f):
    c = git_last_commit(f)
    return _iso_date(c) if c else _iso_date(datetime.now(timezone.utc))


def _infer_type(rel):
    """Frontmatter `type` implied by the path, using the configured directory names."""
    p = rel.parts
    if p[0] == D["entity"] and len(p) >= 2:
        if rel.name == "index.md":
            return ENTITY
        if rel.name == "log.md":
            return "log"
        if D["context"] in p:
            return CONTEXT
        if D["case"] in p:
            return CASE
    if p[0] == D["internal"] and len(p) >= 2 and D["case"] in p:
        return CASE
    if p[0] == D["projects"]:
        return "project"
    if p[0] == D["playbook"]:
        return PLAYBOOK
    if rel.name == "index.md":
        return "index"
    return None


def _infer_entity(rel):
    p = rel.parts
    return p[1] if p[0] == D["entity"] and len(p) >= 2 else ""


def _first_title(text, fallback):
    m = re.search(r"(?m)^#\s+(.+)$", text)
    return m.group(1).strip() if m else fallback


def _first_desc(body):
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or set(s) <= set("|-: "):
            continue
        s = re.sub(r"^[-*]\s+", "", s)
        s = s.replace("**", "").replace("`", "")
        s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
        return s.strip()[:120].strip()
    return ""


def _q(v):
    """Quote a frontmatter value that contains a colon so the naive parser reads it whole."""
    return '"' + v.replace('"', "") + '"' if ":" in v else v


def _set_fm_key(raw, key, value, quote=False):
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        return raw
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return raw
    val = _q(value) if quote else value
    newline = f"{key}: {val}"
    for i in range(1, end):
        if ":" in lines[i] and lines[i].split(":", 1)[0].strip() == key:
            lines[i] = newline
            return "\n".join(lines)
    lines.insert(end, newline)
    return "\n".join(lines)


def apply_fixes():
    """Repair the deterministic failures in place. Returns a list of action strings."""
    actions = []
    for f, rel in kb_files():
        bad = set()
        raw = read_md(f, bad)
        if bad:
            continue      # rewriting would bake the replacement characters into the file
        orig = raw

        # 1. Un-escape literal-\n frontmatter (a corrupted capture write serialised the
        #    whole file as one line with `\n`/`\t` escapes instead of real newlines).
        head = raw.split("\n", 1)[0]
        if raw.lstrip().startswith("---\\n") and "\\n" in head:
            raw = raw.replace("\\n", "\n").replace("\\t", "\t")
            actions.append(f"{rel}: un-escaped literal \\n frontmatter")

        fm, body = split_fm(raw)

        if fm is None:
            # 2. Genuinely missing frontmatter -> build an inferred stub.
            t = _infer_type(rel)
            if not t:
                if raw != orig:
                    f.write_text(raw, encoding="utf-8")
                continue
            ent = _infer_entity(rel)
            title = _first_title(raw, rel.stem.replace("-", " "))
            desc = _first_desc(raw)
            block = ["---", f"type: {t}", f"title: {_q(title)}"]
            if desc:
                block.append(f"description: {_q(desc)}")
            if t in (CONTEXT, "log", CASE) and ent:
                block.append(f"{ENTITY}: {ent}")
            if t == CASE:
                block.append("status: open")
            block += [f"timestamp: {_commit_or_today(f)}", "---"]
            raw = "\n".join(block) + "\n\n" + raw
            actions.append(f"{rel}: added {t} frontmatter stub")
        else:
            t = fm.get("type", "")
            if not fm.get("timestamp"):
                raw = _set_fm_key(raw, "timestamp", _commit_or_today(f))
                actions.append(f"{rel}: added missing timestamp")
            elif t not in ("index", "log"):
                pagets = parse_iso(fm.get("timestamp", ""))
                committed = git_last_commit(f)
                if pagets and committed and committed > pagets + TIMESTAMP_SLACK:
                    raw = _set_fm_key(raw, "timestamp", _iso_date(committed))
                    actions.append(f"{rel}: bumped stale timestamp -> {committed.date()}")
            if not fm.get("description"):
                d = _first_desc(body)
                if d:
                    raw = _set_fm_key(raw, "description", d, quote=True)
                    actions.append(f"{rel}: added missing description")

        if raw != orig:
            f.write_text(raw, encoding="utf-8")
    return actions


def _stale_days_arg(argv):
    """`--stale [DAYS]` -> (True, days) or (False, default). `--stale-fail` is a different flag."""
    if "--stale" not in argv:
        return False, ISSUE_STALE_AFTER.days
    i = argv.index("--stale")
    if i + 1 < len(argv) and argv[i + 1].isdigit():
        return True, int(argv[i + 1])
    return True, ISSUE_STALE_AFTER.days


def stale_report(days: int) -> int:
    """Only the open-case staleness pass: one line per open case whose `timestamp` is older
    than `days`. Used by `leyden leak`. Never fails: this is a to-do list, not a gate."""
    now = datetime.now(timezone.utc)
    cutoff = timedelta(days=days)
    lines = []
    for f, rel in kb_files():
        fm, _ = split_fm(read_md(f))
        if not fm or fm.get("type", "") != CASE:
            continue
        status = fm.get("status", "")
        if status not in OPEN_STATUSES:
            continue
        pagets = parse_iso(fm.get("timestamp", ""))
        if not pagets or now - pagets <= cutoff:
            continue
        age = (now - pagets).days
        lines.append((age, f"{status:<14} {rel.as_posix()}  {age}d since {pagets.date()}"))
    for _, line in sorted(lines, reverse=True):
        print(line)
    if not lines:
        print(f"no open {CASES} untouched for {days}+ days")
    return 0


def main():
    _cfgmod.require_kb()
    stale_only, stale_days = _stale_days_arg(sys.argv[1:])
    if stale_only:
        return stale_report(stale_days)

    stale_fail = "--stale-fail" in sys.argv
    if "--fix" in sys.argv:
        actions = apply_fixes()
        if actions:
            print(f"── --fix applied {len(actions)} repair(s) ──")
            for a in actions:
                print(f"FIXED  {a}")
            print()
        else:
            print("── --fix: nothing to repair ──\n")

    now = datetime.now(timezone.utc)
    fails, warns, stale = [], [], []
    ok = 0
    issue_status = {}   # rel-posix -> status, for the dashboard sync pass

    non_utf8 = set()
    for f, rel in kb_files():
        raw = read_md(f, non_utf8)
        fm, body = split_fm(raw)
        if fm is None:
            fails.append(f"{rel}: no frontmatter")
            continue
        t = fm.get("type", "")
        if not t:
            fails.append(f"{rel}: missing required `type`")
            continue
        if t not in TYPES:
            fails.append(f"{rel}: unknown type `{t}` (expected {sorted(TYPES)})")
            continue

        if not fm.get("description"):
            warns.append(f"{rel}: missing `description` (retrieval hook ranks on it)")
        if not fm.get("timestamp"):
            warns.append(f"{rel}: missing `timestamp`")

        # Template placeholders still in the frontmatter (`leyden new` leaves the ones only a
        # human can fill): WARN, so a fresh scaffold lints clean but the gap stays visible.
        unfilled = sorted(k for k, v in fm.items() if PLACEHOLDER_RE.search(v or ""))
        if unfilled:
            warns.append(f"{rel}: unfilled placeholder in {', '.join(unfilled)}")

        # ── case-specific schema ─────────────────────────────────────────────
        if t == CASE:
            status = fm.get("status", "")
            if status not in ISSUE_STATUSES:
                fails.append(f"{rel}: {CASE} status `{status}` not in {sorted(ISSUE_STATUSES)}")
            else:
                issue_status[rel.as_posix()] = status
            # A case is owned by an entity OR, for internal/product/platform defects, by an
            # `area:` (internal/<cases>/*). One of the two must be present.
            ent = fm.get(ENTITY, "")
            if PLACEHOLDER_RE.search(ent):
                ent = ""          # reported above as an unfilled placeholder
            if not ent and not fm.get("area"):
                warns.append(f"{rel}: {CASE} has neither `{ENTITY}` nor `area` (internal {CASES} "
                             f"use `area:`, e.g. product/<component>)")
            elif ent and not (KB / D["entity"] / ent / "index.md").exists():
                warns.append(f"{rel}: {ENTITY} `{ent}` has no {D['entity']}/{ent}/index.md")
            ctx = fm.get(CONTEXT, "")
            if PLACEHOLDER_RE.search(ctx):
                ctx = ""
            if ctx and ent and not (KB / D["entity"] / ent / D["context"] / f"{ctx}.md").exists():
                warns.append(f"{rel}: {CONTEXT} `{ctx}` not found under "
                             f"{D['entity']}/{ent}/{D['context']}/")
            for slug in _listish(fm.get(PLAYBOOKS, "")):
                if PLACEHOLDER_RE.search(slug):
                    continue
                if not (KB / D["playbook"] / f"{slug}.md").exists():
                    warns.append(f"{rel}: {PLAYBOOK} `{slug}` not found in {D['playbook']}/")

        # entity back-refs on contexts and worklogs
        if t in (CONTEXT, "log"):
            ent = fm.get(ENTITY, "")
            if ent and not (KB / D["entity"] / ent / "index.md").exists():
                warns.append(f"{rel}: {ENTITY} `{ent}` has no {D['entity']}/{ent}/index.md")

        # ── dangling relative links ──────────────────────────────────────────
        # Commented-out markdown is not a link anyone can follow; skip it.
        for m in MD_LINK_RE.finditer(HTML_COMMENT_RE.sub("", body)):
            target = m.group(2)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if PLACEHOLDER_RE.search(target):
                # `[<title>](../<contexts>/<slug>.md)` straight from a template: an unfilled
                # placeholder, not a broken link. It used to hard-FAIL every fresh scaffold.
                warns.append(f"{rel}: unfilled placeholder link `{target}`")
                continue
            resolved = (f.parent / target.split("#")[0]).resolve()
            if resolved.exists():
                continue
            try:
                rel_target = resolved.relative_to(KB).as_posix()
            except ValueError:
                warns.append(f"{rel}: link `{target}` leaves the KB and doesn't exist on disk")
                continue
            # Scope-aware (team edition): a link FROM shared scope (<entities>/) INTO private
            # scope dangles legitimately on a teammate's clone, where the private tree is
            # absent. WARN, never FAIL: otherwise every teammate's lint is permanently red
            # on content that is correct. Same-scope dangling links remain hard FAILs.
            private_top = (D["playbook"], D["internal"], D["projects"], "memory", "workspace-config")
            if rel.as_posix().startswith(D["entity"] + "/") and rel_target.split("/")[0] in private_top:
                warns.append(f"{rel}: cross-scope link `{target}` dangling (private scope; "
                             f"fine on a teammate's clone, fix if broken here)")
            else:
                fails.append(f"{rel}: dangling link `{target}`")

        # ── staleness: content moved past the timestamp ──────────────────────
        # index/log pages churn on every capture, checking them is pure noise
        pagets = parse_iso(fm.get("timestamp", ""))
        if pagets:
            committed = git_last_commit(f) if t not in ("index", "log") else None
            if committed and committed > pagets + TIMESTAMP_SLACK:
                committed = last_content_commit(f)
            if committed and committed > pagets + TIMESTAMP_SLACK:
                stale.append(f"{rel}: last commit {committed.date()} > timestamp "
                             f"{pagets.date()}, update `timestamp:`")
            if t == CASE and fm.get("status") in OPEN_STATUSES \
                    and now - pagets > ISSUE_STALE_AFTER:
                stale.append(f"{rel}: {fm.get('status')} {CASE} untouched since "
                             f"{pagets.date()}, needs a status pass")
        ok += 1

    # ── dashboard sync (open.md vs case statuses) ────────────────────────────
    dash = KB / "open.md"
    if dash.exists():
        text = read_md(dash, non_utf8)
        cut = text.find("## Recently resolved")
        open_section = text if cut < 0 else text[:cut]
        # Per LINE, not per section. A live row legitimately cross-links the resolved sibling
        # cases it grew out of ("[login issue (resolved)](.../old-login-bug.md)"), and a
        # whole-section substring match reported every one of those as "drop it": a warning no
        # tool could clear, because leyden-issues.py --sync-open must not drop a row that still
        # links an open case. A terminal case is only wrongly listed when it sits on a line that
        # links NO open case, which is exactly the row the sync will drop.
        listed, wrongly_listed = set(), set()
        for line in open_section.split("\n"):
            on_line = [r for r in issue_status if r in line]
            if not on_line:
                continue
            listed.update(on_line)
            if any(issue_status[r] in OPEN_STATUSES for r in on_line):
                continue
            wrongly_listed.update(r for r in on_line if issue_status[r] in TERMINAL_STATUSES)
        for rel_posix, status in issue_status.items():
            if status in OPEN_STATUSES and rel_posix not in listed:
                warns.append(f"open.md: {status} {CASE} {rel_posix} missing from the dashboard")
            if rel_posix in wrongly_listed:
                warns.append(f"open.md: lists {rel_posix} but its status is `{status}`, drop it")

    # ── closed.md sync (generated; every terminal-status case must appear there) ──
    closed = KB / "closed.md"
    if closed.exists():
        text = read_md(closed, non_utf8)
        missing = [r for r, st in issue_status.items()
                   if st in TERMINAL_STATUSES and r not in text]
        if missing:
            warns.append(f"closed.md: {len(missing)} closed {CASE}(s) absent ({missing[0]}...), "
                         f"regenerate with `leyden dash`")
        listed_open = [r for r, st in issue_status.items()
                       if st in OPEN_STATUSES and r in text]
        if listed_open:
            warns.append(f"closed.md: lists still-open {CASE}(s) ({listed_open[0]}...), "
                         f"regenerate with `leyden dash`")

    for f in sorted(non_utf8):
        warns.append(f"{f.relative_to(KB)}: not valid UTF-8 (read with replacement characters; "
                     f"re-save it as UTF-8)")
    for w in warns:
        print(f"WARN   {w}")
    for s in stale:
        print(f"STALE  {s}")
    for fl in fails:
        print(f"FAIL   {fl}")
    print(f"\nleyden lint: {ok} ok, {len(warns)} warn, {len(stale)} stale, {len(fails)} fail")
    if fails:
        return 1
    if stale and stale_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
