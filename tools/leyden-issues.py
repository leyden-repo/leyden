#!/usr/bin/env python3
"""
leyden case dashboard: every case, every status, filterable.

`open.md` is the read-me-first dashboard: currently-open work only, hand/hook maintained,
one fat row per case. It deliberately drops cases once they close, so there is no view of
the full history and no way to ask "what did we discard, and why".

This generates that view from the case files themselves (single source of truth =
`status:` in each case's frontmatter, so the dashboard can never drift). "Case" here is
whatever leyden.yaml calls it (issue, blocker, ticket, bug...): every label below is rendered
from the configured vocabulary.

  closed.md    committed, grep-able, retrieval-friendly. The mirror of `open.md` in the same
               visual shape (one fat row per case, title + what happened + links), covering the
               two terminal states: Resolved and Discarded. Carries the counts for all three
               buckets so one file answers "how much is open / closed / dropped".
  issues.html  gitignored, self-contained, no CDN. Every case, every status, in one table:
               filter by bucket, exact status, entity/area and free text; sort any column.

Usage:
    python3 tools/leyden-issues.py                 # writes <kb>/closed.md + <kb>/issues.html
    python3 tools/leyden-issues.py --md-only
    python3 tools/leyden-issues.py --html-only
    python3 tools/leyden-issues.py --check         # exit 1 if closed.md is out of date (CI/lint use)
    python3 tools/leyden-issues.py --brief         # one line per OPEN case, to stdout: the
                                                   # SessionStart injection (a few KB instead of
                                                   # the full open.md); generated straight
                                                   # from case `status:`, so it cannot drift
    python3 tools/leyden-issues.py --sync-open [--fix]
                                                   # reconcile open.md against case files: the
                                                   # narrative cells stay hand/capture-written,
                                                   # but each row's STATUS cell is corrected from
                                                   # the case file, rows whose case reached a
                                                   # terminal status are dropped (closed.md has
                                                   # them), and open cases missing a row are
                                                   # appended minimally. Report-only without --fix.
                                                   # Rows without a [<case>](...) link (log-based
                                                   # asks) are never touched.

stdlib only. Idempotent. Re-run after any KB change (the push hook does this for you).
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import dirs, get as _cfg_get, taxonomy  # noqa: E402

import _config as _cfgmod  # noqa: E402

KB = _cfgmod.KB   # resolved KB (tools/_config.py: LEYDEN_HOME, .kb-path, legacy)
SKIP_DIRS = {"workspace-config", "tools", ".leyden", "_templates", "templates", "team-template", "memory",
             ".git", ".promote-staging"}

# ── vocabulary (from leyden.yaml) ─────────────────────────────────────────────────
T = taxonomy()
D = dirs()
ENTITY, ENTITIES = T["entity"]["singular"], T["entity"]["plural"]
CASE, CASES = T["case"]["singular"], T["case"]["plural"]
PLAYBOOK, PLAYBOOKS = T["playbook"]["singular"], T["playbook"]["plural"]

# Lifecycle buckets. Keep in sync with ISSUE_STATUSES in tools/leyden-lint.py.
OPEN_STATUSES = ["open", "open-diagnosed", "in-progress", "blocked", "workaround", "review"]
CLOSED_STATUSES = ["resolved"]
DISCARDED_STATUSES = ["discarded"]
BUCKETS = [
    ("Open", OPEN_STATUSES, "Still live. Something is owed by someone."),
    ("Closed", CLOSED_STATUSES, "Done and verified. Kept for history, not action."),
    ("Discarded", DISCARDED_STATUSES,
     "Dropped on purpose: not-a-bug, superseded, won't-fix, or the ask went away. "
     "Kept so the decision is not re-litigated."),
]
STATUS_BUCKET = {s: b for b, statuses, _ in BUCKETS for s in statuses}

# Ticket links: provider-agnostic via leyden.yaml `ticketing.url_base`. Empty means "no
# tracker": ids are still shown, just not linked.
TICKET_URL = str(_cfg_get("ticketing.url_base", "") or "")


def split_fm(raw: str):
    """Return (fm_dict, body). Flat `key: value` frontmatter, no YAML dependency."""
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
        if k != k.strip():          # nested key (e.g. under metadata:), flat parse only
            continue
        fm[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return fm, "\n".join(lines[end + 1:])


def read_md(f) -> str:
    """File text; never raises on a non-UTF-8 byte. One stray byte in any .md used to crash this
    script, and the push cycle then silently went without dashboards. The file is named on
    stderr (stdout is the --brief injection) and read with replacement characters."""
    data = f.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        print(f"leyden-issues: {f.relative_to(KB)} is not valid UTF-8 (read with replacement "
              f"characters; re-save it as UTF-8)", file=sys.stderr)
        return data.decode("utf-8", errors="replace")


def case_files():
    for f in sorted(KB.rglob("*.md")):
        rel = f.relative_to(KB)
        if rel.parts[0] in SKIP_DIRS:
            continue
        if f.is_symlink() or not f.is_file():
            continue                     # never follow a link out of the KB; skip dangling ones
        fm, body = split_fm(read_md(f))
        if fm.get("type") == CASE:
            yield rel, fm, body


def pretty(slug: str) -> str:
    return " ".join(w.capitalize() if w.islower() else w for w in slug.split("-"))


def _filled(v) -> str:
    """A frontmatter value, or "" for an unfilled template placeholder (`<...>`) or a YAML null."""
    v = (v or "").strip()
    return "" if v.startswith("<") or v.lower() in ("null", "~", "none") else v


_DISPLAY = {}


def display_name(slug: str) -> str:
    """The entity's own name (title: in <entities>/<slug>/index.md), else the prettified slug,
    so people read "Acme Robotics", not "Acme-robotics"."""
    if slug not in _DISPLAY:
        name = ""
        idx = KB / D["entity"] / slug / "index.md"
        if idx.is_file() and not idx.is_symlink():
            try:
                name = _filled(split_fm(read_md(idx))[0].get("title"))
            except Exception:
                name = ""
        _DISPLAY[slug] = name or pretty(slug)
    return _DISPLAY[slug]


def owner_of(fm, rel) -> str:
    """Who the case belongs to: an entity, or an internal area."""
    if _filled(fm.get(ENTITY)):
        return display_name(fm[ENTITY].strip())
    if _filled(fm.get("area")):
        return fm["area"]
    if len(rel.parts) > 2 and rel.parts[0] == D["entity"]:
        return display_name(rel.parts[1])          # filed under an entity folder, field missing
    # internal/<cases>/<slug>.md with no area declared
    return "internal" if rel.parts[0] == D["internal"] else "unassigned"


def ticket_of(fm) -> str:
    """`ticket_id:` with the legacy `linear_id:` accepted as a fallback. YAML's empty spellings
    (null, ~, none) and an unfilled template placeholder (<TICKET-XXX>) mean "no ticket": they
    used to render literally, as "null Acme" in the session brief."""
    for key in ("ticket_id", "linear_id"):
        v = (fm.get(key) or "").strip()
        if v and v.lower() not in ("null", "~", "none", "n/a", "-") and not v.startswith("<"):
            return v
    return ""


def ticket_link_md(tid: str) -> str:
    return f"[{tid}]({TICKET_URL}{tid})" if TICKET_URL else f"`{tid}`"


def collect():
    rows = []
    for rel, fm, _ in case_files():
        status = fm.get("status", "") or "open"
        rows.append({
            "path": rel.as_posix(),
            "title": fm.get("title") or rel.stem,
            "description": fm.get("description", ""),
            "status": status,
            "bucket": STATUS_BUCKET.get(status, "Open"),
            "owner": owner_of(fm, rel),
            "internal": rel.parts[0] != D["entity"],
            "ticket": ticket_of(fm),
            "updated": (fm.get("timestamp", "") or "")[:10],
            "playbooks": [s.strip() for s in fm.get(PLAYBOOKS, "").strip("[]").split(",") if s.strip()],
        })
    rows.sort(key=lambda r: (r["updated"] or "0000-00-00"), reverse=True)
    return rows


# ── markdown ────────────────────────────────────────────────────────────────────

def render_closed_md(rows, today):
    """Render closed.md: the mirror of open.md, same fat-row shape, terminal states only."""
    counts = {b: sum(1 for r in rows if r["bucket"] == b) for b, _, _ in BUCKETS}
    out = [
        "---",
        "type: index",
        f"title: Closed {CASES} - dashboard",
        f"description: Every {CASE} that is done or deliberately dropped, resolved and discarded, "
        f"with what happened. The mirror of open.md. Generated from each {CASE}'s `status:` by "
        "tools/leyden-issues.py - do not hand-edit.",
        f"timestamp: {today}T00:00:00Z",
        "---",
        "",
        f"# Closed {CASES} (dashboard)",
        "",
        "The mirror of [`open.md`](open.md): everything that is finished or deliberately dropped, "
        "kept so a past decision can be found instead of re-litigated. `open.md` is what you read "
        "at session start; this is the history behind it.",
        "",
        "**Generated file, do not hand-edit.** The source of truth for every row is the `status:` "
        f"in the {CASE} file itself, so this cannot drift. Regenerate with "
        "`python3 tools/leyden-issues.py` (the batched push hook does it for you).",
        "",
        f"| Bucket | Count | Where |",
        "|--------|-------|-------|",
        f"| Open | {counts['Open']} | [open.md](open.md) |",
        f"| Resolved | {counts['Closed']} | below |",
        f"| Discarded | {counts['Discarded']} | below |",
        "",
        f"Total **{len(rows)}** {CASES} on file: {sum(1 for r in rows if r['internal'])} internal, "
        f"{sum(1 for r in rows if not r['internal'])} {ENTITY}. For filtering and sorting across "
        f"all statuses at once, open `issues.html` (gitignored, regenerated alongside this file).",
        "",
        f"Dates are each {CASE}'s last meaningful update, which for a closed {CASE} is when it "
        "closed.",
    ]

    sections = [
        ("Resolved", CLOSED_STATUSES,
         "Done and verified. Kept for history, not action."),
        ("Discarded", DISCARDED_STATUSES,
         "Dropped on purpose: not-a-bug, superseded, won't-fix, or the ask went away."),
    ]
    for heading, statuses, blurb in sections:
        srows = [r for r in rows if r["status"] in statuses]
        out += ["", f"## {heading} ({len(srows)})", "", f"_{blurb}_", ""]
        if not srows:
            out += ["None yet.", ""]
            continue
        out += [f"| {ENTITY.capitalize()} / area | {CASE.capitalize()} | Closed | Link |",
                "|-----------------|-------|--------|------|"]
        for r in srows:
            links = [f"[{CASE}]({r['path']})"]
            if r["ticket"]:
                links.append(ticket_link_md(r["ticket"]))
            links += [f"[{PLAYBOOK}: {pb}]({D['playbook']}/{pb}.md)" for pb in r["playbooks"]]
            if not r["internal"]:
                ent = r["path"].split("/")[1]
                # not every entity folder has a worklog yet, so don't emit a dangling link
                if (KB / D["entity"] / ent / "log.md").exists():
                    links.append(f"[log]({D['entity']}/{ent}/log.md)")
            desc = f" {r['description']}" if r["description"] else ""
            out.append(f"| {r['owner']} | **{r['title']}**{desc} | {r['updated'] or '-'} | "
                       f"{' · '.join(links)} |")
    out.append("")
    return "\n".join(out)


# ── html ────────────────────────────────────────────────────────────────────────

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>leyden {cases}</title>
<style>
:root {{ --bg:#fbfbfa; --fg:#22201d; --muted:#6b6660; --line:#e5e1db; --card:#fff;
         --open:#b45309; --closed:#15803d; --discarded:#6b7280; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#191817; --fg:#eae7e2; --muted:#9a948c; --line:#332f2b; --card:#211f1d;
           --open:#f59e0b; --closed:#4ade80; --discarded:#9ca3af; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
        font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
.wrap {{ max-width:1180px; margin:0 auto; }}
h1 {{ font-size:1.35rem; margin:0 0 .25rem; }}
.sub {{ color:var(--muted); font-size:.85rem; margin-bottom:1.5rem; }}
.controls {{ display:flex; flex-wrap:wrap; gap:.5rem; align-items:center; margin-bottom:1rem; }}
button, select, input {{ font:inherit; color:var(--fg); background:var(--card);
        border:1px solid var(--line); border-radius:7px; padding:.4rem .7rem; }}
button {{ cursor:pointer; }}
button.on {{ background:var(--fg); color:var(--bg); border-color:var(--fg); }}
input[type=search] {{ flex:1; min-width:200px; }}
table {{ width:100%; border-collapse:collapse; font-size:.9rem; }}
th, td {{ text-align:left; padding:.55rem .5rem; border-bottom:1px solid var(--line);
          vertical-align:top; }}
th {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.05em; color:var(--muted);
      cursor:pointer; white-space:nowrap; }}
td.title {{ width:52%; }}
td.title a {{ color:var(--fg); text-decoration:none; font-weight:600; }}
td.title a:hover {{ text-decoration:underline; }}
td.title p {{ margin:.2rem 0 0; color:var(--muted); font-size:.82rem; }}
.pill {{ display:inline-block; padding:.1rem .45rem; border-radius:999px; font-size:.74rem;
         border:1px solid currentColor; white-space:nowrap; }}
.b-Open {{ color:var(--open); }} .b-Closed {{ color:var(--closed); }}
.b-Discarded {{ color:var(--discarded); }}
.tag {{ font-size:.74rem; color:var(--muted); }}
.count {{ color:var(--muted); font-size:.82rem; margin:.75rem 0; }}
.hidden {{ display:none; }}
@media (max-width:700px) {{ td.title {{ width:auto; }} .col-opt {{ display:none; }} }}
</style></head><body><div class="wrap">
<h1>leyden {cases}</h1>
<div class="sub">{total} {cases} · generated {today} from each {case}'s <code>status:</code> ·
regenerate with <code>python3 tools/leyden-issues.py</code></div>
<div class="controls">
  <button data-bucket="all" class="on">All</button>
  <button data-bucket="Open">Open</button>
  <button data-bucket="Closed">Closed</button>
  <button data-bucket="Discarded">Discarded</button>
  <select id="status"><option value="">any status</option>{status_opts}</select>
  <select id="owner"><option value="">any {entity} / area</option>{owner_opts}</select>
  <select id="kind"><option value="">{entity} + internal</option>
    <option value="{entity}">{entity} only</option><option value="internal">internal only</option></select>
  <input type="search" id="q" placeholder="search title, description, path">
</div>
<div class="count" id="count"></div>
<table><thead><tr>
  <th data-sort="title">{Case}</th>
  <th data-sort="owner">{Entity} / area</th>
  <th data-sort="status">Status</th>
  <th data-sort="ticket" class="col-opt">Ticket</th>
  <th data-sort="updated">Updated</th>
</tr></thead><tbody id="rows">{rows}</tbody></table>
</div><script>
const rows = [...document.querySelectorAll('#rows tr')];
const state = {{ bucket:'all', status:'', owner:'', kind:'', q:'' }};
function apply() {{
  let n = 0;
  for (const tr of rows) {{
    const d = tr.dataset;
    const hay = (d.title + ' ' + d.description + ' ' + d.path + ' ' + d.owner).toLowerCase();
    const show = (state.bucket === 'all' || d.bucket === state.bucket)
      && (!state.status || d.status === state.status)
      && (!state.owner  || d.owner === state.owner)
      && (!state.kind   || d.kind === state.kind)
      && (!state.q      || hay.includes(state.q));
    tr.classList.toggle('hidden', !show);
    if (show) n++;
  }}
  document.getElementById('count').textContent = n + ' of ' + rows.length + ' shown';
}}
document.querySelectorAll('[data-bucket]').forEach(b => b.onclick = () => {{
  document.querySelectorAll('[data-bucket]').forEach(x => x.classList.remove('on'));
  b.classList.add('on'); state.bucket = b.dataset.bucket; apply();
}});
document.getElementById('status').onchange = e => {{ state.status = e.target.value; apply(); }};
document.getElementById('owner').onchange  = e => {{ state.owner  = e.target.value; apply(); }};
document.getElementById('kind').onchange   = e => {{ state.kind   = e.target.value; apply(); }};
document.getElementById('q').oninput       = e => {{ state.q = e.target.value.toLowerCase(); apply(); }};
let asc = {{}};
document.querySelectorAll('th[data-sort]').forEach(th => th.onclick = () => {{
  const k = th.dataset.sort; asc[k] = !asc[k];
  const body = document.getElementById('rows');
  rows.sort((a, b) => (a.dataset[k] || '').localeCompare(b.dataset[k] || '') * (asc[k] ? 1 : -1))
      .forEach(tr => body.appendChild(tr));
}});
apply();
</script></body></html>
"""


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_html(rows, today):
    statuses = [s for _, ss, _ in BUCKETS for s in ss if any(r["status"] == s for r in rows)]
    owners = sorted({r["owner"] for r in rows}, key=str.lower)
    tr = []
    for r in rows:
        if r["ticket"] and TICKET_URL:
            ticket = f'<a href="{esc(TICKET_URL)}{esc(r["ticket"])}">{esc(r["ticket"])}</a>'
        elif r["ticket"]:
            ticket = esc(r["ticket"])
        else:
            ticket = '<span class="tag">-</span>'
        desc = f'<p>{esc(r["description"])}</p>' if r["description"] else ""
        tr.append(
            f'<tr data-bucket="{r["bucket"]}" data-status="{esc(r["status"])}" '
            f'data-owner="{esc(r["owner"])}" data-kind="{"internal" if r["internal"] else esc(ENTITY)}" '
            f'data-title="{esc(r["title"])}" data-description="{esc(r["description"])}" '
            f'data-path="{esc(r["path"])}" data-ticket="{esc(r["ticket"])}" '
            f'data-updated="{esc(r["updated"])}">'
            f'<td class="title"><a href="{esc(r["path"])}">{esc(r["title"])}</a>{desc}'
            f'<div class="tag">{esc(r["path"])}</div></td>'
            f'<td>{esc(r["owner"])}</td>'
            f'<td><span class="pill b-{r["bucket"]}">{esc(r["status"])}</span></td>'
            f'<td class="col-opt">{ticket}</td><td>{esc(r["updated"])}</td></tr>')
    return HTML.format(
        total=len(rows), today=today,
        cases=esc(CASES), case=esc(CASE), Case=esc(CASE.capitalize()),
        entity=esc(ENTITY), Entity=esc(ENTITY.capitalize()),
        status_opts="".join(f'<option value="{s}">{s}</option>' for s in statuses),
        owner_opts="".join(f'<option value="{esc(o)}">{esc(o)}</option>' for o in owners),
        rows="\n".join(tr))


# open.md rows link their case as `[<case>](path)`. `issue` is always accepted too, so a KB
# that predates the configurable vocabulary keeps working without a rewrite.
# Any markdown link whose TARGET is a case file, whatever the link text says.
# It used to require the link text to be literally `[issue]`, which only ever matched the minimal
# rows this script appends itself. Every hand- or capture-written row links its cases descriptively
# ("[login issue (resolved)](customers/acme/issues/...)"), so the sync could not see those rows at
# all: status cells were never corrected and terminal rows were never dropped, while the lint (which
# scans every link) kept reporting the drift. The two now look at the same thing.
CASE_LINK_RE = re.compile(
    r"\[[^\]]*\]\(([^)\s]*(?:" + re.escape(D["case"]) + r")/[^)\s]+\.md)\)")


def sync_open(rows, fix: bool) -> int:
    """Reconcile open.md's table rows against case-file `status:` (the single source of truth).
    Full generation was rejected on purpose: open.md rows carry hand/capture-written narrative
    far richer than any frontmatter, and several rows are log-based asks with no case file at
    all. So the sync is surgical: status cell corrected, terminal rows dropped, missing open
    cases appended as minimal rows, everything else untouched. Structurally ambiguous rows
    (cell count != 5) are reported and skipped, never rewritten."""
    open_path = KB / "open.md"
    if not open_path.exists():
        print("no open.md")
        return 0
    by_path = {r["path"]: r for r in rows}
    linked_paths = set()
    changed = 0
    out_lines = []
    for line in read_md(open_path).split("\n"):
        s = line.strip()
        targets = CASE_LINK_RE.findall(line)
        if not (s.startswith("|") and targets):
            out_lines.append(line)
            continue
        # normalize each link target before lookup: './'-prefixes and '#anchors' are valid
        # markdown but broke the exact-match lookup, ROW-SKIPping the row AND re-appending the
        # same case as a permanent duplicate.
        known, unknown = [], []
        for t in targets:
            t = t.split("#")[0]
            while t.startswith("./"):
                t = t[2:]
            (known.append(by_path[t]) if t in by_path else unknown.append(t))
        if not known:
            for t in unknown:
                print(f"ROW-SKIP  {CASE} link points at no known {CASE} file: {t}")
            out_lines.append(line)
            continue
        # A fat row routinely links several cases: its own, plus the resolved siblings it grew
        # out of. All of them count as "on the dashboard", but only the OPEN ones decide the
        # row's fate: dropping a live row because its FIRST link happened to be a resolved
        # sibling would delete hand-written narrative that exists nowhere else.
        linked_paths.update(c["path"] for c in known)
        open_cases = [c for c in known if c["bucket"] == "Open"]
        cells = [c.strip() for c in s.strip("|").split(" | ")]
        if len(cells) == 4:
            # the "Recently resolved" table (Entity | Case | Date | Link): fine as long as
            # the linked cases really are terminal; flag it if one is still open
            for c in open_cases:
                print(f"DRIFT     {c['path']} is {c['status']} but sits in a "
                      f"recently-resolved row: fix the {CASE} status or the row by hand")
            out_lines.append(line)
            continue
        if len(cells) != 5:
            print(f"ROW-SKIP  not a clean 5-column row ({len(cells)} cells): {cells[0][:40]}")
            out_lines.append(line)
            continue
        if not open_cases:
            print(f"DROP      every {CASE} this row links is terminal "
                  f"({', '.join(c['path'] for c in known)}): row removed (see closed.md)")
            changed += 1
            continue
        # Correct the status cell only when the row's open case is unambiguous. With two open
        # cases on one row there is no single right answer, and guessing would overwrite a
        # deliberate hand-written summary status.
        if len(open_cases) > 1:
            if cells[2] not in {c["status"] for c in open_cases}:
                print(f"AMBIGUOUS row links {len(open_cases)} open {CASES} "
                      f"({', '.join(c['path'] for c in open_cases)}); status cell "
                      f"'{cells[2]}' left as written")
            out_lines.append(line)
            continue
        case = open_cases[0]
        if cells[2] != case["status"]:
            print(f"STATUS    {case['path']}: '{cells[2]}' -> '{case['status']}'")
            cells[2] = case["status"]
            changed += 1
            out_lines.append("| " + " | ".join(cells) + " |")
            continue
        out_lines.append(line)
    missing = [r for r in rows if r["bucket"] == "Open" and r["path"] not in linked_paths]
    if missing:
        # Insert INTO the open table, not at EOF: end-of-file is below "## Recently resolved",
        # where lint's dashboard check (which cuts at that header) never sees the row, so it
        # reported "missing from dashboard" forever and --fix could never converge.
        insert_at = next((i for i, l in enumerate(out_lines)
                          if l.strip().lower().startswith("## recently resolved")), len(out_lines))
        while insert_at > 0 and not out_lines[insert_at - 1].strip():
            insert_at -= 1
        for r in missing:
            print(f"APPEND    {r['path']} ({r['status']}) has no open.md row")
            desc = f": {r['description']}" if r["description"] else ""
            out_lines.insert(insert_at, f"| {r['owner']} | **{r['title']}**{desc} | {r['status']} "
                                        f"| - | [{CASE}]({r['path']}) |")
            insert_at += 1
            changed += 1
    if not changed:
        print(f"open.md in sync with {CASE} files")
        return 0
    if fix:
        open_path.write_text("\n".join(out_lines), encoding="utf-8")
        print(f"open.md updated ({changed} changes)")
    else:
        print(f"{changed} changes needed, rerun with --fix to apply")
    return 0


def render_brief(rows):
    """The SessionStart injection: every open case on one line. Full detail lives one search
    away (the UserPromptSubmit hook surfaces complete entries on demand), so the per-session
    context tax stays flat as the entity count grows."""
    open_rows = [r for r in rows if r["bucket"] == "Open"]
    counts = {b: sum(1 for r in rows if r["bucket"] == b) for b, _, _ in BUCKETS}
    out = [f"{counts['Open']} open ({counts['Closed']} resolved, {counts['Discarded']} discarded "
           f"on file). One line each; full table: {KB.name}/open.md"]
    for r in open_rows:
        # Label rule: who + what first, ticket ids last in parentheses.
        ticket = f" ({r['ticket']})" if r["ticket"] else ""
        who = r["owner"]
        if r["internal"]:
            who = "Internal" if r["owner"] in ("internal", "unassigned") else f"Internal: {r['owner']}"
        out.append(f"- {who}: {r['title']}{ticket} [{r['status']}]  ({r['path']})")
    return "\n".join(out)


def main():
    _cfgmod.require_kb()
    args = sys.argv[1:]
    today = datetime.now(timezone.utc).date().isoformat()
    rows = collect()
    md_path, html_path = KB / "closed.md", KB / "issues.html"

    if "--brief" in args:
        print(render_brief(rows))
        return 0

    if "--sync-open" in args:
        return sync_open(rows, fix="--fix" in args)

    if "--check" in args:
        want = render_closed_md(rows, today)
        have = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        # ignore only the generated timestamp line when comparing
        strip = lambda t: re.sub(r"^timestamp: .*$", "", t, flags=re.M)
        if strip(want) != strip(have):
            print("closed.md is out of date, run: python3 tools/leyden-issues.py")
            return 1
        print(f"closed.md up to date ({len(rows)} {CASES})")
        return 0

    if "--html-only" not in args:
        md_path.write_text(render_closed_md(rows, today), encoding="utf-8")
        print(f"wrote {md_path.relative_to(KB)} ({len(rows)} {CASES})")
    if "--md-only" not in args:
        html_path.write_text(render_html(rows, today), encoding="utf-8")
        print(f"wrote {html_path.relative_to(KB)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
