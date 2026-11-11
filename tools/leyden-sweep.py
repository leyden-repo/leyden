#!/usr/bin/env python3
"""
leyden stale-case sweep: find open cases nobody has touched for a while, and apply the
decisions a human made about them.

Two phases, and the human always decides:
  1. GATHER (scheduled via tools/leyden-sweep-gather.sh, or inside /kb-sweep): `list` gives the
     stale cases with a plain-language summary and search hints; a sandboxed, read-only model run
     collects evidence from Gmail/Slack/Linear and writes a report into <KB>/.leyden/sweep/.
     Case files are never edited in this phase.
  2. REVIEW + APPLY (interactive /kb-sweep): the human ticks what to close; `apply` writes only
     the ticked decisions, then re-syncs open.md and regenerates the dashboards.

Usage:
    leyden sweep list [--days 14] [--limit N] [--json]
    leyden sweep apply <decisions.json> [--dry-run]
    leyden sweep status          # one line for the SessionStart hook, or nothing
    leyden sweep mark-gathered [<report.json>]  # stamp .last-gather (the gather wrapper calls it)
    leyden sweep schedule-label                 # launchd label for this KB (slug + path hash)
    leyden sweep schedule | unschedule          # weekly launchd gather (bin/leyden handles these)

Paths use the KB's own vocabulary (leyden.yaml): <entities>/<slug>/<cases>/<file>.md and
internal/<cases>/<file>.md. The examples below use the deployment preset words.

decisions.json: [{"path": "customers/x/issues/y.md", "action": "resolve"|"keep",
                  "note": "evidence in one or two sentences", "followup": "YYYY-MM-DD" (optional)}]

stdlib only, python 3.9+.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config as _cfgmod  # noqa: E402
from _config import dirs, taxonomy  # noqa: E402

KB = _cfgmod.KB                      # resolved KB (LEYDEN_HOME, <tool>/.kb-path, legacy)
STATE = _cfgmod.STATE / "sweep"      # <KB>/.leyden/sweep: reports, decisions, stamps, cron log
TOOLS = Path(__file__).resolve().parent
_T, _D = taxonomy(), dirs()
ENTITY, CASE = _T["entity"]["singular"], _T["case"]["singular"]
ENT_DIR, CASE_DIR, INT_DIR = _D["entity"], _D["case"], _D["internal"]
TERMINAL = {"resolved", "discarded"}
DUE_DAYS = 7
TICKET_RE = re.compile(r"\b(?:[A-Z]{2,6}-\d{2,6})\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


FM_KEY_RE = re.compile(r"""^(["']?)([A-Za-z0-9_-]+)\1\s*:(.*)$""")


def _norm(raw: str) -> str:
    """BOM and CRLF tolerant view of a file (the write path keeps LF)."""
    return raw.lstrip("\ufeff").replace("\r\n", "\n")


def split_fm(raw: str):
    raw = _norm(raw)
    if not raw.startswith("---"):
        return {}, raw, None
    lines = raw.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, raw, None
    fm = {}
    for line in lines[1:end]:
        if not line.strip() or line[:1].isspace() or line.lstrip().startswith("#"):
            continue                       # blank, nested (indented) or comment line
        m = FM_KEY_RE.match(line)
        if m:
            # Drop an inline " # comment" (as leyden-issues.py does): "resolved  # open | ..." read as open.
            v = m.group(3)
            if m.group(2) == "status":
                v = v.split(" #")[0]   # template comment after the status value; titles may contain " #"
            fm[m.group(2)] = v.strip().strip('"').strip("'")
    return fm, "\n".join(lines[end + 1:]), end


def issue_paths():
    pats = [f"{ENT_DIR}/*/{CASE_DIR}/*.md", f"{INT_DIR}/{CASE_DIR}/*.md"]
    for p in pats:
        for f in sorted(KB.glob(p)):
            yield f


def customer_name(slug: str) -> str:
    idx = KB / ENT_DIR / slug / "index.md"
    if idx.exists():
        fm, _, _ = split_fm(idx.read_text(encoding="utf-8"))
        if fm.get("title"):
            return fm["title"]
    return " ".join(w.capitalize() for w in slug.split("-"))


def clean_ticket(v: str) -> str:
    v = (v or "").strip().strip('"').strip("'")
    m = TICKET_RE.search(v)
    return m.group(0) if m else ""


def plain(md: str) -> str:
    """Markdown to one line of plain text. Strips markdown SYNTAX only, so identifiers such as
    max_retry_count, #42 or 2.3.1 -> 2.4.0 survive."""
    s = re.sub(r"```.*?```", " ", md, flags=re.S)
    s = re.sub(r"`([^`\n]*)`", r"\1", s)
    s = re.sub(r"\[\[([^\]|]+)\|?([^\]]*)\]\]", lambda m: m.group(2) or m.group(1), s)
    s = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.M)            # heading markers
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.M)                 # blockquote markers
    s = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", s, flags=re.M)     # list bullets
    s = re.sub(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", " ", s, flags=re.M)  # table rule rows
    s = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"\2", s)        # bold
    s = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"\1", s)  # *italic*
    s = s.replace("|", " ")
    s = s.replace('\\"', '"').replace(" \u2014 ", ": ").replace("\u2014", ", ").replace("\u2013", "-")
    return re.sub(r"\s+", " ", s).strip()


def clip(s: str, n: int = 280) -> str:
    """Cut at a word boundary, never mid-word."""
    if len(s) <= n:
        return s
    cut = s[:n - 3]
    if " " in cut[n // 2:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "..."


def summarise(fm: dict, body: str) -> str:
    desc = plain(fm.get("description", ""))
    if len(desc) >= 40:
        out = desc
    else:
        m = re.search(r"\*\*Summary\*\*\s*\n((?:\s*[-*].*\n?){1,3})", body)
        if not m:
            m = re.search(r"^##\s*Summary\s*\n+((?:\s*[-*].*\n?){1,3})", body, re.M)
        if m:
            out = plain(" ".join(l.strip().lstrip("-* ") + "." for l in m.group(1).splitlines() if l.strip()))
        else:
            paras = [p for p in re.split(r"\n\s*\n", body) if p.strip() and not p.lstrip().startswith(("#", "|", "```"))]
            out = plain(paras[0]) if paras else desc
        if desc and desc not in out:
            out = desc + ". " + out
    out = re.sub(r"(?<![./])\.\.(?=\s|$)", ".", out)   # sentence-end ".." only: "../runbooks/x" stays intact
    return clip(out)


def search_hints(cust: str, fm: dict, body: str) -> list:
    hints = []
    if cust:
        hints.append(cust)
    t = clean_ticket(fm.get("linear_id", "") or fm.get("ticket_id", ""))
    for x in [t] + TICKET_RE.findall(body):
        if x and x not in hints:
            hints.append(x)
    for e in EMAIL_RE.findall(body):
        if e not in hints and not e.endswith(".md"):
            hints.append(e)
    for name in re.findall(r"\*\*(?:Reported by|Customer contact|Contact)[^*]*\*\*:?\s*([A-Z][a-z]+(?: [A-Z][a-z]+)?)", body):
        if name not in hints:
            hints.append(name)
    for code in re.findall(r"`([^`\n]+)`", body):
        code = code.strip()
        if not 8 <= len(code) <= 80:
            continue
        if " " in code or re.search(r"[A-Za-z]{4,}", code):
            if code not in hints and not code.endswith(".md"):
                hints.append(code)
        if len(hints) >= 12:
            break
    return hints[:12]


def age_days(ts: str, now: datetime):
    """Whole days since an ISO date or datetime (any offset). None when missing or unparseable."""
    ts = (ts or "").strip()
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        try:
            t = datetime.strptime(ts[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int((now - t).total_seconds() // 86400)


def stale(days: int, now: datetime):
    out = []
    for f in issue_paths():
        if f.is_symlink() or not f.resolve().is_relative_to(KB.resolve()):
            continue                                   # never read through a link out of the KB
        raw = f.read_text(encoding="utf-8", errors="replace")
        fm, body, _ = split_fm(raw)
        if fm.get("type", CASE) != CASE:
            continue
        status = fm.get("status", "") or "open"
        if status in TERMINAL:
            continue
        ts = fm.get("timestamp", "") or ""
        d = age_days(ts, now)
        if d is not None and d < days:
            continue
        rel = f.relative_to(KB).as_posix()
        if rel.startswith(ENT_DIR + "/"):
            cust = customer_name(rel.split("/")[1])
        else:
            cust = "Internal: " + (fm.get("area") or "general")
        out.append({
            "path": rel, "entity": cust, "title": plain(fm.get("title") or f.stem),
            "status": status, "days_since": d, "last_updated": ts[:10],
            "ticket": clean_ticket(fm.get("linear_id", "") or fm.get("ticket_id", "")),
            "summary": summarise(fm, body), "search_hints": search_hints(cust, fm, body),
        })
    out.sort(key=lambda r: -(r["days_since"] if r["days_since"] is not None else 10 ** 6))
    return out


def _int_opt(args, name, default):
    """Value of `name N`, with an argparse-style error instead of a traceback when N is missing or bad."""
    if name not in args:
        return default
    i = args.index(name)
    try:
        return int(args[i + 1])
    except (IndexError, ValueError):
        print(f"usage: leyden sweep list [--days N] [--limit N] [--json]\nerror: {name} needs an integer", file=sys.stderr)
        raise SystemExit(2)


def cmd_list(args):
    as_json = "--json" in args
    days, limit = _int_opt(args, "--days", 14), _int_opt(args, "--limit", 0)
    rows = stale(days, datetime.now(timezone.utc))
    if limit:
        rows = rows[:limit]
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    print(f"{len(rows)} open {_T['case']['plural']} untouched for {days}+ days")
    for r in rows:
        tk = f" ({r['ticket']})" if r["ticket"] else ""
        age = f"{r['days_since']}d" if r["days_since"] is not None else "no timestamp"
        print(f"- {r['entity']}: {r['title']}{tk}  [{r['status']}, {age}]")
        print(f"    {r['summary']}")
        print(f"    {r['path']}")
    return 0


def _allowed(rel: str) -> bool:
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or p.suffix != ".md":
        return False
    parts = p.parts
    return (len(parts) == 4 and parts[0] == ENT_DIR and parts[2] == CASE_DIR) or \
           (len(parts) == 3 and parts[0] == INT_DIR and parts[1] == CASE_DIR)


STATUS_LINE_RE = re.compile(r"""^(["']?)status\1\s*:""")
TS_LINE_RE = re.compile(r"""^(["']?)timestamp\1\s*:""")


def _inside_kb(f: Path) -> bool:
    """True only when f and every folder between it and the KB are real (no symlinks) and the
    resolved path stays inside the KB."""
    kb = KB.resolve()
    p = f
    while True:
        if p.is_symlink():
            return False
        if p.parent == p or p == KB:
            break
        p = p.parent
    try:
        return f.resolve(strict=True).is_relative_to(kb)
    except (OSError, RuntimeError):
        return False


def apply_one(d: dict, today: str):
    """Plan one decision. Returns (file, old, new, change) where change is one of
    'resolved', 'noted', 'skipped:<reason>'. Raises SystemExit on anything unsafe."""
    rel = d.get("path", "")
    if not isinstance(rel, str) or not _allowed(rel):
        raise SystemExit(f"refused: {rel!r} is not a {CASE} file under {ENT_DIR}/*/{CASE_DIR} or {INT_DIR}/{CASE_DIR}")
    action = d.get("action")
    if action not in ("resolve", "keep"):
        raise SystemExit(f"refused: action must be resolve or keep, got {action!r} for {rel}")
    f = KB / rel
    if not f.is_file() or not _inside_kb(f):
        raise SystemExit(f"refused: {rel} is missing, a symlink, or resolves outside the KB")
    raw = f.read_text(encoding="utf-8")
    norm = _norm(raw)
    fm, body, end = split_fm(raw)
    if end is None:
        raise SystemExit(f"refused: {rel} has no frontmatter")
    lines = norm.split("\n")
    note = (d.get("note") or "").strip().replace(" \u2014 ", ": ").replace("\u2014", ", ")
    already = (fm.get("status") or "").strip().lower() in TERMINAL
    if action == "resolve" and already:
        return f, raw, raw, "skipped:already resolved"
    text = ("Resolved. " if action == "resolve" else "Still open. ") + note
    if d.get("followup"):
        text += f" Follow up by {d['followup']}."
    section = f"## Sweep {today}\n\n{text}\n"
    if section.strip() in norm:
        return f, raw, raw, "skipped:same note already applied today"
    if action == "resolve":
        hit = False
        for i in range(1, end):
            if STATUS_LINE_RE.match(lines[i]):
                lines[i] = "status: resolved"
                hit = True
        if not hit:
            lines.insert(end, "status: resolved")
            end += 1
    ts_hit = False
    for i in range(1, end):
        if TS_LINE_RE.match(lines[i]):
            lines[i] = f"timestamp: {today}T00:00:00Z"
            ts_hit = True
    if not ts_hit:
        lines.insert(end, f"timestamp: {today}T00:00:00Z")
    new = "\n".join(lines).rstrip("\n") + f"\n\n{section}"
    return f, raw, new, ("resolved" if action == "resolve" else "noted")


def _mark_reviewed(today: str):
    """Stamp .last-sweep and mark every gathered report reviewed (ends the SessionStart nudge)."""
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / ".last-sweep").write_text(datetime.now(timezone.utc).isoformat() + "\n")
    for rep in STATE.glob("report-*.json"):
        reviewed = rep.with_suffix(".reviewed")
        if not reviewed.exists():
            reviewed.write_text(today + "\n")


def cmd_apply(args):
    if not args:
        print("usage: apply <decisions.json> [--dry-run]", file=sys.stderr)
        return 2
    dry = "--dry-run" in args
    try:
        decisions = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:   # missing or unparseable file: a one-line error, not a traceback
        print(f"usage: apply <decisions.json> [--dry-run]\nerror: cannot read {args[0]}: {e}", file=sys.stderr)
        return 2
    if not isinstance(decisions, list) or not all(isinstance(d, dict) for d in decisions):
        raise SystemExit("refused: decisions file must be a JSON list of objects")
    seen = set()
    for d in decisions:
        p = d.get("path")
        if p in seen:
            raise SystemExit(f"refused: {p!r} appears twice in the decisions file")
        seen.add(p)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    planned = [apply_one(d, today) for d in decisions]   # validate everything before writing anything
    import difflib
    for f, old, new, change in planned:
        if change.startswith("skipped:"):
            print(f"skip {f.relative_to(KB)}: {change.split(':', 1)[1]}")
            continue
        if dry:
            sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                       str(f.relative_to(KB)), str(f.relative_to(KB)), n=1))
        else:
            f.write_text(new, encoding="utf-8")
    n_res = sum(1 for p in planned if p[3] == "resolved")
    n_note = sum(1 for p in planned if p[3] == "noted")
    n_skip = len(planned) - n_res - n_note
    if dry:
        print(f"\n(dry run: {n_res} would be resolved, {n_note} noted, {n_skip} skipped; nothing written)")
        return 0
    if n_res + n_note == 0:
        _mark_reviewed(today)   # all skipped or an empty list is still a finished review: clear the nudge
        print("nothing changed (all decisions already applied)")
        return 0
    py = sys.executable or "/usr/bin/python3"
    for cmd in ([py, str(TOOLS / "leyden-issues.py"), "--sync-open", "--fix"],
                [py, str(TOOLS / "leyden-issues.py")],
                [py, str(TOOLS / "leyden-graph.py")]):
        subprocess.run(cmd, cwd=str(KB), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    _mark_reviewed(today)
    print(f"applied: {n_res} resolved, {n_note} kept with a note, {n_skip} skipped; "
          "open.md synced, dashboards regenerated")
    return 0


def _age_days(p: Path):
    try:
        t = datetime.fromisoformat(p.read_text().strip().replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).days
    except Exception:
        return None


REPORT_FRESH_DAYS = 7      # same rule as the skill: an unreviewed report older than this is ignored


def _pending_report():
    """Newest unreviewed, parseable report under REPORT_FRESH_DAYS old: (n, m) or None."""
    today = datetime.now(timezone.utc).date()
    for r in sorted(STATE.glob("report-*.json"), reverse=True):
        if r.with_suffix(".reviewed").exists():
            continue
        m = re.match(r"report-(\d{4}-\d{2}-\d{2})", r.name)
        try:
            if not m or (today - datetime.strptime(m.group(1), "%Y-%m-%d").date()).days >= REPORT_FRESH_DAYS:
                continue
            data = json.loads(r.read_text(encoding="utf-8"))
            items = data.get("issues") if isinstance(data, dict) else data
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                continue
        except Exception:
            continue                                   # a broken report never hides the due nudge
        return len(items), sum(1 for i in items
                               if str(i.get("verdict", "")).upper().startswith("LIKELY RESOLVED"))
    return None


def cmd_status(_args):
    try:
        pend = _pending_report()
        if pend:
            n, m = pend
            print(f"KB sweep ready: {n} stale {_T['case']['plural']} reviewed, {m} look resolved. Run /kb-sweep to review.")
            return 0
    except Exception:
        pass
    try:
        ages = [a for a in (_age_days(STATE / ".last-sweep"), _age_days(STATE / ".last-gather")) if a is not None]
        age = min(ages) if ages else None      # a recent gather with nothing to review is not "due"
        if age is None and _scheduled():
            # Just scheduled, first gather not run yet: not "due", and not "never run" either.
            print(f"Weekly KB sweep scheduled: first run {_next_monday_9()}.")
            return 0
        if age is None or age >= DUE_DAYS:
            n = len(stale(14, datetime.now(timezone.utc)))
            if n:
                when = f"last run {age}d ago" if age is not None else "never run"
                print(f"Weekly KB sweep due ({when}, {n} {_T['case']['plural']} untouched 14+ days): run /kb-sweep.")
    except Exception:
        pass
    return 0


def _label():
    import hashlib
    slug = re.sub(r"[^a-z0-9-]+", "-", KB.name.lower()).strip("-") or "kb"
    return f"com.leyden.kb-sweep.{slug}-{hashlib.sha1(str(KB.resolve()).encode('utf-8')).hexdigest()[:8]}"


def _scheduled():
    """True when `leyden sweep schedule` installed this KB's launchd job."""
    return (Path.home() / "Library" / "LaunchAgents" / f"{_label()}.plist").is_file()


def _next_monday_9():
    """Local time of the next Monday 09:00 launchd fire, e.g. 'Mon 12 Oct 09:00'."""
    from datetime import timedelta
    now = datetime.now()
    d = (0 - now.weekday()) % 7
    nxt = (now + timedelta(days=d)).replace(hour=9, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=7)
    return nxt.strftime("%a %d %b 09:00")


def cmd_schedule_label(_args):
    """launchd label for this KB: folder slug + a hash of the full KB path, so two KBs with the
    same folder name (e.g. two "notes" folders) never overwrite each other's job."""
    print(_label())
    return 0


def cmd_mark_gathered(args):
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / ".last-gather").write_text(datetime.now(timezone.utc).isoformat() + "\n")
    return 0


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if a[0] != "status":
        _cfgmod.require_kb()
    elif _cfgmod.KB_SOURCE == "none":
        return 0
    cmds = {"list": cmd_list, "apply": cmd_apply, "status": cmd_status, "mark-gathered": cmd_mark_gathered,
            "schedule-label": cmd_schedule_label}
    if a[0] not in cmds:
        print(f"unknown command {a[0]!r}", file=sys.stderr)
        return 2
    return cmds[a[0]](a[1:])


if __name__ == "__main__":
    sys.exit(main())
