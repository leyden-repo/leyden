#!/usr/bin/env python3
"""
Generate the KB map: one self-contained HTML page with four views of the knowledge base.

  <Entities>    one row per <entity>, its <cases> as status-coloured chips, most open first
  <Playbook> reuse  which <playbooks> are linked from the most <cases>, and for which <entities>
  Related       a small graph around one page (search or click), 1 or 2 hops
  Overview      the whole graph, labels on hover, <cases> pulled toward their <entity>

Edges come from relative markdown links, [[wiki-links]] (full id or unique slug), and the
frontmatter relations <case>.<entity> / <case>.<context> / <case>.<playbooks> / <context>.<entity>.
A shared reader panel renders any page. The footer lists data gaps (<cases> with no <playbook>,
links to missing pages, unresolved wiki-links) so the notes can be fixed. Same counts go to stderr.

Output is a single HTML file (d3 + marked from a CDN; needs internet to render).

Usage:
    python3 tools/leyden-graph.py               # writes <kb>/graph.html (gitignored)
    python3 tools/leyden-graph.py -o out.html   # custom output path

stdlib only, Python 3.9+. Idempotent. The push cycle re-runs it.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import taxonomy  # noqa: E402

import _config as _cfgmod  # noqa: E402

KB = _cfgmod.KB   # resolved KB (tools/_config.py: LEYDEN_HOME, .kb-path, legacy)

# ── vocabulary (from leyden.yaml) ─────────────────────────────────────────────
_T = taxonomy()
CFG = {
    "entity": _T["entity"]["singular"], "entities": _T["entity"]["plural"],
    "context": _T["context"]["singular"], "contexts": _T["context"]["plural"],
    "case": _T["case"]["singular"], "cases": _T["case"]["plural"],
    "playbook": _T["playbook"]["singular"], "playbooks": _T["playbook"]["plural"],
    "kb_name": str(_cfgmod.get("kb.name", "") or "Leyden KB"),
    "ticket_keys": ["ticket_id", "linear_id"],
}

# ── scanner ───────────────────────────────────────────────────────────────────
# Everything below this line is the generic scanner; only the CFG block above is configuration.

SKIP_DIRS = {"workspace-config", "tools", ".leyden", "_templates", "templates", "team-template",
             ".git", ".promote-staging", "scratch", "tmp"}
SKIP_FILES = {"README.md", "CONVENTIONS.md"}
# Root-level .md is KB content only for these (same rule as leyden-lint.py / leyden-index.py
# ROOT_KB_FILES): SETUP.md, reminders.md and other root docs are not graph nodes.
ROOT_KB_FILES = {"index.md", "open.md", "closed.md", "log.md"}
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
WIKI_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]*))?\]\]")
FENCE_RE = re.compile(r"^(```|~~~)")
CODE_SPAN_RE = re.compile(r"(`+[^`]*`+)")
DONE = {"resolved", "discarded"}


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
        if s and s[0] != "'":
            s = s.replace('\\"', '"')
    return s


def _scalar(v: str) -> str:
    """One YAML-ish scalar: quoted values keep '#'; unquoted ones lose a ' #' comment."""
    v = v.strip()
    if v[:1] in ("\"", "'"):
        q = v[0]
        i = 1
        while i < len(v):
            if v[i] == "\\" and q == "\"":
                i += 2
                continue
            if v[i] == q:
                return _unquote(v[:i + 1])
            i += 1
        return _unquote(v)
    if v.startswith("#"):
        return ""
    cut = re.search(r"\s#", v)
    return (v[:cut.start()] if cut else v).strip()


def _inline_list(v: str) -> list:
    inner = v.strip()[1:-1]
    out, cur, q = [], "", None
    for ch in inner:
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "\"'" and not cur.strip():
            q = ch
            cur += ch
        elif ch == ",":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [x for x in (_unquote(i) for i in out) if x]


def split_fm(raw: str):
    """Return (fm, body). Top-level `key: value`, inline `[a, b]` lists and block `- item` lists.
    No YAML dependency; nested mappings are skipped."""
    if not raw.startswith("---"):
        return {}, raw
    lines = raw.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, raw
    fm: dict = {}
    key = None
    for ln in lines[1:end]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if ln[0] in " \t":
            item = ln.strip()
            if key is not None and item.startswith("- "):
                val = fm.get(key)
                if not isinstance(val, list):
                    val = [] if val in ("", None) else [val]
                v = _scalar(item[2:])
                if v:
                    val.append(v)
                fm[key] = val
            continue
        if ":" not in ln:
            key = None
            continue
        k, _, v = ln.partition(":")
        key = k.strip()
        v = v.strip()
        if v.startswith("[") and v.rstrip().endswith("]"):
            fm[key] = _inline_list(v)
        else:
            fm[key] = _scalar(v)
    return fm, "\n".join(lines[end + 1:]).lstrip("\n")


def fstr(fm: dict, key: str) -> str:
    v = fm.get(key, "")
    return ", ".join(v) if isinstance(v, list) else str(v)


def flist(fm: dict, key: str) -> list:
    v = fm.get(key, "")
    if isinstance(v, list):
        return v
    v = str(v).strip()
    if v.startswith("[") and v.endswith("]"):
        return _inline_list(v)
    return [s.strip() for s in v.split(",") if s.strip()] if v else []


def node_id(path: Path) -> str:
    rel = str(path.relative_to(KB))
    return rel[:-3] if rel.endswith(".md") else rel


def kb_files():
    for f in sorted(KB.rglob("*.md")):
        rel = f.relative_to(KB)
        if any(p in SKIP_DIRS for p in rel.parts[:-1]) or f.name in SKIP_FILES:
            continue
        if len(rel.parts) == 1 and f.name not in ROOT_KB_FILES:
            continue
        if rel.parts[0].startswith("."):
            continue
        yield f


def short_title(title: str) -> str:
    t = title.replace("`", "").strip().strip('"')
    for sep in (" \u2014 ", " -- ", " | "):
        if sep in t:
            head, rest = t.split(sep, 1)
            # keep the qualifier when the head alone says little ("Acme", "Billing API")
            t = head if len(head) >= 28 else head + ": " + rest
            break
    t = t.strip()
    return t if len(t) <= 64 else t[:61].rstrip() + "..."


def _link_href(nid: str) -> str:
    return "#n:" + quote(nid, safe="/-_.~")


def scan_kb():
    """Two passes: read every page, then resolve links once every id is known."""
    E, ES = CFG["entity"], CFG["entities"]
    C, CS = CFG["context"], CFG["contexts"]
    K, KS = CFG["case"], CFG["cases"]
    P, PS = CFG["playbook"], CFG["playbooks"]
    types = [E, C, K, P, "project", "log", "index"]

    pages = []
    for f in kb_files():
        try:
            raw = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fm, body = split_fm(raw)
        nid = node_id(f)
        ntype = fstr(fm, "type") or "index"
        if ntype not in types:
            ntype = "index"
        title = fstr(fm, "title")
        if not title:
            for line in body.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
        title = title or f.stem
        parts = nid.split("/")
        ent = fstr(fm, E)
        if not ent and len(parts) >= 3 and parts[0] == ES:
            ent = parts[1]
        if ntype == E and len(parts) >= 2 and parts[0] == ES:
            ent = parts[1]
        ticket = ""
        for tk in CFG["ticket_keys"]:
            ticket = ticket or fstr(fm, tk)
        dtype = fstr(fm, C + "_type") or fstr(fm, C + "_types")
        pages.append((f, nid, ntype, title, fm, body, ent, ticket, dtype))

    ids = {p[1] for p in pages}
    by_base: dict = {}
    for nid in ids:
        by_base.setdefault(nid.rsplit("/", 1)[-1], []).append(nid)

    edges, dangling, unresolved_wiki = [], [], []

    def resolve_wiki(target: str, here: Path):
        t = target.strip().split("#")[0].strip()
        if t.endswith(".md"):
            t = t[:-3]
        while t.startswith("./"):
            t = t[2:]
        t = t.lstrip("/")
        if not t:
            return None
        if t in ids:
            return t
        try:
            rel = node_id((here / (t + ".md")).resolve())
            if rel in ids:
                return rel
        except ValueError:
            pass
        if f"{ES}/{t}/index" in ids:
            return f"{ES}/{t}/index"
        base = t.rsplit("/", 1)[-1]
        cands = by_base.get(base, [])
        if len(cands) == 1:
            return cands[0]
        if f"{PS}/{base}" in ids:
            return f"{PS}/{base}"
        return None

    nodes = {}
    for f, nid, ntype, title, fm, body, ent, ticket, dtype in pages:
        def add(target, via):
            if target == nid:
                return
            if target in ids:
                edges.append({"source": nid, "target": target, "via": via})
            else:
                dangling.append({"source": nid, "target": target, "via": via})

        def rewrite_md(m, base=f.parent):
            label, target = m.group(1), m.group(2)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                return m.group(0)
            path = target.split("#")[0]
            if not path.endswith(".md"):
                return m.group(0)
            try:
                tid = node_id((base / path).resolve())
            except ValueError:            # points outside the KB
                return m.group(0)
            add(tid, "link")
            return f"[{label or tid}]({_link_href(tid)})"

        def rewrite_wiki(m, here=f.parent):
            target, label = m.group(1), m.group(2)
            tid = resolve_wiki(target, here)
            text = (label or target).strip()
            if tid is None:
                unresolved_wiki.append({"source": nid, "target": target.strip()})
                return f"[{text}](#x:{quote(target.strip(), safe='/-_.~')})"
            add(tid, "wiki")
            return f"[{text}]({_link_href(tid)})"

        out_lines, in_fence = [], False
        for line in body.split("\n"):
            if FENCE_RE.match(line.lstrip()):
                in_fence = not in_fence
            elif not in_fence:
                segs = CODE_SPAN_RE.split(line)   # odd items are `inline code`: leave as written
                for i in range(0, len(segs), 2):
                    segs[i] = MD_LINK_RE.sub(rewrite_md, WIKI_RE.sub(rewrite_wiki, segs[i]))
                line = "".join(segs)
            out_lines.append(line)
        body = "\n".join(out_lines)

        # frontmatter relations
        fm_ent = flist(fm, E)
        for e in fm_ent:
            add(f"{ES}/{e}/index", E)
        if ntype == K:
            for ctx in flist(fm, C):
                if ent:
                    add(f"{ES}/{ent}/{CS}/{ctx}", C)
            for slug in flist(fm, PS):
                add(f"{PS}/{slug}", P)
        elif ntype == C and ent and not fm_ent:
            add(f"{ES}/{ent}/index", E)

        nodes[nid] = {
            "id": nid, "t": ntype, "title": title, "short": short_title(title),
            "status": fstr(fm, "status"), "ts": fstr(fm, "timestamp")[:10],
            "ent": ent, "area": fstr(fm, "area"), "ctx": fstr(fm, C),
            "pbs": flist(fm, PS), "ticket": ticket, "tags": flist(fm, "tags"),
            "rank": fstr(fm, "rank"), "last_used": fstr(fm, "last_used")[:10],
            "dtype": dtype, "body": body,
        }
    return nodes, edges, dangling, unresolved_wiki


def build_graph_data(nodes, edges):
    degree = {nid: 0 for nid in nodes}
    seen, unique = set(), []
    for e in edges:
        a, b = e["source"], e["target"]
        if a == b or (a, b) in seen or (b, a) in seen:
            continue
        seen.add((a, b))
        unique.append({"source": a, "target": b})
        degree[a] += 1
        degree[b] += 1
    node_list = []
    for nid, info in nodes.items():
        d = dict(info)
        d["degree"] = degree[nid]
        node_list.append(d)
    return node_list, unique


def edge_kinds(nodes, links) -> dict:
    out: dict = {}
    for e in links:
        k = f"{nodes[e['source']]['t']}->{nodes[e['target']]['t']}"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def script_json(obj) -> str:
    r"""json.dumps, made safe to inline inside an HTML <script> block.

    The page embeds every page's BODY, so any KB entry that quotes HTML puts that HTML inside the
    script tag. A literal `</script>` in the data would end the block there and the browser would
    render the rest of the payload as page text (this happened: a review note quoting the bug
    broke the graph). `<\/` is valid JSON and parses back to `</`; `<!--` does the same for a
    comment opener. Both keep the payload STRICTLY valid JSON.
    """
    return (json.dumps(obj, indent=None, ensure_ascii=False)
            .replace("</", "<\\/")
            .replace("<!--", "\\u003c!--"))


def render(output: Path) -> None:
    nodes, edges, dangling, unresolved = scan_kb()
    node_list, links = build_graph_data(nodes, edges)
    K, P = CFG["case"], CFG["playbook"]
    adj: dict = {n: set() for n in nodes}
    for e in links:
        adj[e["source"]].add(e["target"])
        adj[e["target"]].add(e["source"])
    no_pb = sorted(n for n, v in nodes.items()
                   if v["t"] == K and not any(nodes[m]["t"] == P for m in adj[n]))
    payload = {
        "nodes": node_list, "links": links,
        "dangling": dangling, "unresolved": unresolved, "noPlaybook": no_pb,
        "cfg": {k: CFG[k] for k in ("entity", "entities", "context", "contexts", "case", "cases",
                                    "playbook", "playbooks", "kb_name")},
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    import base64
    import hashlib
    import html as _html
    html = HTML_TEMPLATE.replace("__PAYLOAD__", script_json(payload)) \
                        .replace("__KB_NAME__", _html.escape(CFG["kb_name"]))
    # CSP: the one inline script is allowed by its own hash, nothing else inline can run.
    start = html.index("<script>\n") + len("<script>")
    end = html.index("</script>", start)
    digest = base64.b64encode(hashlib.sha256(html[start:end].encode("utf-8")).digest()).decode()
    html = html.replace("__SCRIPT_HASH__", "sha256-" + digest, 1)
    output.write_text(html, encoding="utf-8")
    size_kb = output.stat().st_size // 1024
    kinds = edge_kinds(nodes, links)
    err = sys.stderr
    print(f"Graph: {len(node_list)} nodes, {len(links)} edges, {size_kb} KB -> {output}")
    print("  edges by kind: " + ", ".join(f"{k} {v}" for k, v in kinds.items()), file=err)
    print(f"  {K} pages with no {P} link: {len(no_pb)}", file=err)
    dt: dict = {}
    for d in dangling:
        dt[d["target"]] = dt.get(d["target"], 0) + 1
    print(f"  dangling links: {len(dangling)} ({len(dt)} distinct targets)", file=err)
    for t, n in sorted(dt.items(), key=lambda kv: -kv[1]):
        print(f"    {n}x {t}", file=err)
    uw: dict = {}
    for d in unresolved:
        uw[d["target"]] = uw.get(d["target"], 0) + 1
    print(f"  unresolved [[wiki-links]]: {len(unresolved)} ({len(uw)} distinct)", file=err)
    for t, n in sorted(uw.items(), key=lambda kv: -kv[1]):
        print(f"    {n}x [[{t}]]", file=err)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src https://cdnjs.cloudflare.com '__SCRIPT_HASH__'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; font-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>__KB_NAME__ map</title>
<style>
/* Layout: sticky header + tabs; each view is a full-width working surface; reader is a right drawer. */
:root {
  --bg: #f6f6f3; --surface: #ffffff; --raised: #fbfbf9; --fg: #1d2226; --muted: #5d666d; --faint: #8a9298;
  --line: #dddfd9; --accent: #2f6f7a; --accent-soft: #e0eef0; --focus: #2f6f7a;
  --ty-entity: #2b62a8; --ty-context: #7a4fba; --ty-case: #b5532b; --ty-playbook: #2f855a;
  --ty-project: #a8740f; --ty-log: #8a9095; --ty-index: #a3a8ac;
  --st-open: #cf3a32; --st-open-diagnosed: #dd7a1c; --st-in-progress: #2e6db4; --st-blocked: #9a2f96;
  --st-review: #11868a; --st-workaround: #a88a0a; --st-resolved: #3b8a4e; --st-discarded: #8d949a; --st-other: #8d949a;
  --shadow: 0 8px 28px rgba(20, 30, 35, 0.14);
  --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #131719; --surface: #1a1f22; --raised: #20262a; --fg: #e3e6e3; --muted: #9aa3a9; --faint: #6f787e;
    --line: #2d3438; --accent: #6fc0c8; --accent-soft: #1d3337; --focus: #6fc0c8;
    --ty-entity: #6aa5ef; --ty-context: #b493ec; --ty-case: #ec8a5e; --ty-playbook: #5cc28a;
    --ty-project: #e0b04a; --ty-log: #8e979c; --ty-index: #6c757b;
    --st-open: #f0645c; --st-open-diagnosed: #f2a04a; --st-in-progress: #63a2ee; --st-blocked: #d06ccc;
    --st-review: #40c2c2; --st-workaround: #e0c53e; --st-resolved: #5fbf75; --st-discarded: #7d868c; --st-other: #7d868c;
    --shadow: 0 8px 28px rgba(0, 0, 0, 0.5);
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #131719; --surface: #1a1f22; --raised: #20262a; --fg: #e3e6e3; --muted: #9aa3a9; --faint: #6f787e;
  --line: #2d3438; --accent: #6fc0c8; --accent-soft: #1d3337; --focus: #6fc0c8;
  --ty-entity: #6aa5ef; --ty-context: #b493ec; --ty-case: #ec8a5e; --ty-playbook: #5cc28a;
  --ty-project: #e0b04a; --ty-log: #8e979c; --ty-index: #6c757b;
  --st-open: #f0645c; --st-open-diagnosed: #f2a04a; --st-in-progress: #63a2ee; --st-blocked: #d06ccc;
  --st-review: #40c2c2; --st-workaround: #e0c53e; --st-resolved: #5fbf75; --st-discarded: #7d868c; --st-other: #7d868c;
  --shadow: 0 8px 28px rgba(0, 0, 0, 0.5);
  color-scheme: dark;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { margin: 0; }
body { background: var(--bg); color: var(--fg); font: 14px/1.5 var(--sans); }
button { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.wrap { padding-inline: 20px; }

/* header */
header.top { position: sticky; top: 0; z-index: 30; background: var(--bg); border-bottom: 1px solid var(--line); }
.bar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 24px; padding-block: 12px 8px; }
.brand { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; min-width: 0; }
.brand h1 { font-size: 18px; margin: 0; font-weight: 650; letter-spacing: -0.01em; }
.counts { margin: 0; color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.counts b { color: var(--fg); font-weight: 600; }
.spacer { flex: 1; }
.search { position: relative; width: min(360px, 100%); }
.search input { width: 100%; padding: 7px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); color: var(--fg); font: inherit; }
.results { position: absolute; top: calc(100% + 4px); left: 0; right: 0; z-index: 40; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); max-height: 60vh; overflow-y: auto; padding: 4px; }
.res { display: flex; gap: 8px; align-items: baseline; width: 100%; text-align: left; background: none; border: 0; padding: 6px 8px; border-radius: 5px; cursor: pointer; }
.res:hover, .res.active { background: var(--accent-soft); }
.res .tt { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.res .ty { font-size: 11px; color: var(--muted); }
.theme-btn { border: 1px solid var(--line); background: var(--surface); border-radius: 6px; padding: 6px 10px; cursor: pointer; color: var(--muted); font-size: 12px; }
nav.tabs { display: flex; gap: 2px; overflow-x: auto; }
nav.tabs button { background: none; border: 0; border-bottom: 2px solid transparent; padding: 8px 12px; cursor: pointer; color: var(--muted); font-weight: 500; white-space: nowrap; }
nav.tabs button[aria-selected="true"] { color: var(--fg); border-bottom-color: var(--accent); }

main { padding-block: 16px 24px; }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 18px; margin-bottom: 14px; color: var(--muted); font-size: 13px; }
.toolbar label { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
.lead { margin: 0 0 14px; max-width: 80ch; color: var(--muted); }
.lead b { color: var(--fg); }
h2.sec { font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; margin: 26px 0 10px; }

/* legend + status */
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; }
.lg { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }
.lg i, .chip i { width: 8px; height: 8px; border-radius: 50%; background: var(--c); flex: none; display: inline-block; }
.lg b { color: var(--fg); font-weight: 600; font-variant-numeric: tabular-nums; }

/* customers board */
.rows { display: grid; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); overflow: hidden; }
.row { display: grid; grid-template-columns: 230px minmax(0, 1fr); border-top: 1px solid var(--line); }
.row:first-child { border-top: 0; }
.rh { padding: 12px 14px; border-right: 1px solid var(--line); background: var(--raised); display: grid; gap: 3px; align-content: start; min-width: 0; }
.rh .nm { background: none; border: 0; padding: 0; text-align: left; font-weight: 600; cursor: pointer; font-size: 14px; }
.rh .nm:hover { color: var(--accent); }
.rh .sub { font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.rh .cnt { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.rh .cnt b { color: var(--fg); }
.chips { padding: 10px 12px; display: flex; flex-wrap: wrap; gap: 6px; align-content: flex-start; min-width: 0; }
.area { width: 100%; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--faint); margin-top: 4px; }
.area:first-child { margin-top: 0; }
.chip { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; padding: 4px 9px 4px 8px; border: 1px solid color-mix(in srgb, var(--c) 45%, var(--line)); background: color-mix(in srgb, var(--c) 9%, var(--surface)); border-radius: 999px; cursor: pointer; font-size: 12.5px; text-align: left; }
.chip:hover { border-color: var(--c); }
.chip .txt { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.chip.done { opacity: 0.55; }
.quiet { margin-top: 12px; color: var(--muted); font-size: 13px; }
.quiet button { background: none; border: 0; padding: 0; color: var(--fg); cursor: pointer; text-decoration: underline; text-decoration-color: var(--line); text-underline-offset: 3px; }

/* reuse */
.bars { display: grid; gap: 6px; }
.brow { display: grid; grid-template-columns: minmax(0, 340px) minmax(0, 1fr); gap: 12px; align-items: center; }
.brow .lbl { background: none; border: 0; padding: 0; text-align: right; cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.brow .lbl:hover { color: var(--accent); }
.track { display: flex; align-items: center; gap: 10px; min-width: 0; }
.barbox { flex: 1 1 auto; min-width: 40px; max-width: 520px; }
.fill { display: block; height: 14px; border-radius: 3px; background: var(--ty-playbook); }
.val { font-size: 12px; color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
.val b { color: var(--fg); }
.matrix-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }
table.matrix { border-collapse: collapse; font-size: 12.5px; }
table.matrix th, table.matrix td { border-bottom: 1px solid var(--line); padding: 0; }
table.matrix thead th { vertical-align: bottom; padding: 8px 2px; font-weight: 500; color: var(--muted); }
table.matrix thead th span { writing-mode: vertical-rl; transform: rotate(180deg); white-space: nowrap; display: inline-block; max-height: 150px; overflow: hidden; text-overflow: ellipsis; }
table.matrix tbody th .linkish { display: block; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
table.matrix tbody th { text-align: left; font-weight: 400; padding: 5px 12px; white-space: nowrap; position: sticky; left: 0; background: var(--surface); max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
table.matrix td { width: 30px; min-width: 30px; height: 28px; text-align: center; font-variant-numeric: tabular-nums; border-left: 1px solid var(--line); }
table.matrix td.on { background: color-mix(in srgb, var(--ty-playbook) var(--p), transparent); color: var(--fg); cursor: default; font-weight: 600; }
table.matrix tbody tr:last-child th, table.matrix tbody tr:last-child td { border-bottom: 0; }
details.never { margin-top: 16px; }
details.never summary { cursor: pointer; color: var(--muted); }
details.never ul { columns: 2 280px; padding-left: 18px; margin: 8px 0 0; }
details.never li button, .linkish { background: none; border: 0; padding: 0; cursor: pointer; color: var(--fg); text-align: left; }
details.never li button:hover, .linkish:hover { color: var(--accent); }

/* graphs */
.graph-box { position: relative; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); overflow: hidden; }
.graph-box svg { display: block; width: 100%; touch-action: none; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.seg button { border: 0; background: var(--surface); padding: 4px 10px; cursor: pointer; color: var(--muted); }
.seg button[aria-pressed="true"] { background: var(--accent-soft); color: var(--fg); font-weight: 600; }
.focus-title { color: var(--fg); font-weight: 600; }
.empty { padding: 28px 24px; display: grid; gap: 12px; }
.empty p { margin: 0; color: var(--muted); }
.starts { display: grid; gap: 6px; padding: 0; margin: 0; list-style: none; }
.starts button { display: inline-flex; gap: 8px; align-items: baseline; }
.starts small { color: var(--faint); font-variant-numeric: tabular-nums; }
svg text { font-family: var(--sans); }
.nlabel { font-size: 11.5px; fill: var(--fg); paint-order: stroke; stroke: var(--surface); stroke-width: 3.5px; stroke-linejoin: round; pointer-events: none; }
.nlabel.focus { font-size: 13px; font-weight: 650; }
.edge { stroke: var(--faint); stroke-opacity: 0.45; }
.edge.hot { stroke: var(--accent); stroke-opacity: 0.9; }
.node { cursor: pointer; }
.dim { opacity: 0.15; }
.note { font-size: 12px; color: var(--faint); }

/* footer */
footer { border-top: 1px solid var(--line); padding-block: 14px 28px; color: var(--muted); font-size: 12.5px; display: grid; gap: 6px; }
footer details summary { cursor: pointer; }
footer details ul { margin: 6px 0 4px; padding-left: 18px; columns: 2 380px; }
footer code { font-family: var(--mono); font-size: 11.5px; }

/* tooltip + reader */
.tip { position: fixed; z-index: 60; pointer-events: none; background: var(--surface); border: 1px solid var(--line); border-radius: 6px; box-shadow: var(--shadow); padding: 8px 10px; font-size: 12px; max-width: 340px; }
.tip .t1 { font-weight: 600; margin-bottom: 2px; }
.tip .t2 { color: var(--muted); }
.reader { position: fixed; top: 0; right: 0; bottom: 0; width: min(620px, 100vw); z-index: 50; background: var(--surface); border-left: 1px solid var(--line); box-shadow: var(--shadow); display: flex; flex-direction: column; }
.rd-head { display: flex; align-items: center; gap: 8px; padding: 12px 14px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
.rd-head .ttl { flex: 1 1 200px; font-weight: 600; min-width: 0; }
.rd-btn { border: 1px solid var(--line); background: var(--surface); border-radius: 5px; padding: 3px 9px; cursor: pointer; font-size: 13px; }
.rd-btn:disabled { opacity: 0.35; cursor: default; }
.badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 600; color: #fff; background: var(--c); }
.pill { font-size: 11px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--c); color: var(--fg); background: color-mix(in srgb, var(--c) 14%, transparent); }
.rd-meta { padding: 8px 18px; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 12px; display: flex; flex-wrap: wrap; gap: 4px 14px; }
.rd-body { flex: 1; overflow-y: auto; padding: 16px 20px 60px; line-height: 1.6; overflow-wrap: anywhere; }
.rd-body h1 { font-size: 20px; margin: 0 0 12px; }
.rd-body h2 { font-size: 16px; margin: 20px 0 8px; }
.rd-body h3 { font-size: 14px; margin: 16px 0 6px; }
.rd-body p, .rd-body ul, .rd-body ol, .rd-body pre, .rd-body table, .rd-body blockquote { margin: 0 0 12px; }
.rd-body ul, .rd-body ol { padding-left: 22px; }
.rd-body code { font-family: var(--mono); font-size: 12.5px; background: var(--raised); padding: 1px 4px; border-radius: 3px; }
.rd-body pre { background: var(--raised); padding: 10px 12px; border-radius: 6px; overflow-x: auto; }
.rd-body pre code { background: none; padding: 0; }
.rd-body table { border-collapse: collapse; display: block; overflow-x: auto; font-size: 13px; }
.rd-body th, .rd-body td { border: 1px solid var(--line); padding: 4px 8px; text-align: left; }
.rd-body blockquote { border-left: 3px solid var(--line); padding-left: 12px; color: var(--muted); }
.rd-body a { color: var(--accent); }
.rd-body a.kb { text-decoration: none; border-bottom: 1px dashed var(--accent); }
.rd-body a.missing { color: var(--st-open); border-bottom: 1px dashed var(--st-open); text-decoration: none; cursor: help; }

@media (max-width: 720px) {
  .wrap { padding-inline: 16px; }
  .row { grid-template-columns: minmax(0, 1fr); }
  .rh { border-right: 0; border-bottom: 1px solid var(--line); }
  .brow { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  .brow .lbl { text-align: left; }
  .search { width: 100%; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<header class="top">
  <div class="wrap bar">
    <div class="brand"><h1>__KB_NAME__</h1><p class="counts" id="counts"></p></div>
    <div class="spacer"></div>
    <div class="search">
      <input id="q" type="search" placeholder="Search pages" autocomplete="off" aria-label="Search pages">
      <div class="results" id="results" hidden></div>
    </div>
    <button class="theme-btn" id="theme" type="button" title="Switch theme">Theme: system</button>
  </div>
  <nav class="tabs wrap" role="tablist" aria-label="Views">
    <button role="tab" data-view="customers" aria-selected="true" id="tab-customers"></button>
    <button role="tab" data-view="reuse" aria-selected="false" id="tab-reuse"></button>
    <button role="tab" data-view="related" aria-selected="false">Related</button>
    <button role="tab" data-view="overview" aria-selected="false">Overview</button>
  </nav>
</header>

<main class="wrap">
  <section id="v-customers" class="view">
    <div class="toolbar">
      <label><input type="checkbox" id="show-done"> <span id="show-done-lbl">Show resolved</span></label>
      <div class="legend" id="legend"></div>
    </div>
    <div class="rows" id="cust-rows"></div>
    <div class="quiet" id="cust-quiet"></div>
  </section>

  <section id="v-reuse" class="view" hidden>
    <p class="lead" id="reuse-lead"></p>
    <h2 class="sec" id="bars-h"></h2>
    <div class="bars" id="bars"></div>
    <h2 class="sec" id="matrix-h"></h2>
    <div class="matrix-wrap"><table class="matrix" id="matrix"></table></div>
    <details class="never" id="never"><summary></summary><ul></ul></details>
  </section>

  <section id="v-related" class="view" hidden>
    <div class="toolbar">
      <span>Connections of <span class="focus-title" id="rel-title">nothing selected</span></span>
      <span class="seg" role="group" aria-label="Depth">
        <button type="button" data-depth="1" aria-pressed="true">1 hop</button>
        <button type="button" data-depth="2" aria-pressed="false">2 hops</button>
      </span>
      <label><input type="checkbox" id="rel-meta"> Include index and worklog pages</label>
      <span class="note" id="rel-note"></span>
    </div>
    <div class="graph-box">
      <div class="empty" id="rel-empty"></div>
      <svg id="rel-svg" hidden></svg>
    </div>
  </section>

  <section id="v-overview" class="view" hidden>
    <div class="toolbar" id="ov-tools"></div>
    <div class="graph-box"><svg id="ov-svg"></svg></div>
  </section>
</main>

<footer class="wrap" id="foot"></footer>

<aside class="reader" id="reader" hidden aria-label="Page reader">
  <div class="rd-head">
    <button class="rd-btn" id="rd-back" type="button" title="Back" disabled>Back</button>
    <span class="ttl" id="rd-title"></span>
    <button class="rd-btn" id="rd-rel" type="button">Show connections</button>
    <button class="rd-btn" id="rd-close" type="button" title="Close (Esc)">Close</button>
  </div>
  <div class="rd-meta" id="rd-meta"></div>
  <div class="rd-body" id="rd-body"></div>
</aside>
<div class="tip" id="tip" hidden></div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js" integrity="sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i" crossorigin="anonymous"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/11.1.1/marked.min.js" integrity="sha384-zbcZAIxlvJtNE3Dp5nxLXdXtXyxwOdnILY1TDPVmKFhl4r4nSUG1r8bcFXGVa4Te" crossorigin="anonymous"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.7/purify.min.js" integrity="sha384-XQqX/4yiUGu+oyr87jvWzRuqBUK/adrY0DunhL+tID9m/9dwSpV8h9Fk/Sg6ifVQ" crossorigin="anonymous"></script>
<script>
const DATA = __PAYLOAD__;
const CFG = DATA.cfg;
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const cap = s => s ? s[0].toUpperCase() + s.slice(1) : s;
const plural = (n, one, many) => n + " " + (n === 1 ? one : many);

const nodes = DATA.nodes;
const byId = new Map(nodes.map(n => [n.id, n]));
const adj = new Map(nodes.map(n => [n.id, new Set()]));
DATA.links.forEach(l => { adj.get(l.source).add(l.target); adj.get(l.target).add(l.source); });

const SLOT = {};
SLOT[CFG.entity] = "entity"; SLOT[CFG.context] = "context"; SLOT[CFG.case] = "case"; SLOT[CFG.playbook] = "playbook";
SLOT.project = "project"; SLOT.log = "log"; SLOT.index = "index";
const TYPE_ORDER = [CFG.entity, CFG.context, CFG.case, CFG.playbook, "project", "log", "index"];
const typeLabel = t => t === "log" ? "Worklog" : cap(t);
const typeVar = t => "var(--ty-" + (SLOT[t] || "index") + ")";
const STATUSES = ["open", "open-diagnosed", "in-progress", "blocked", "review", "workaround", "resolved", "discarded"];
const stKey = s => STATUSES.includes(s) ? s : "other";
const stVar = s => "var(--st-" + stKey(s) + ")";
const isDone = s => s === "resolved" || s === "discarded";
const isCase = n => n.t === CFG.case;
const META = new Set(["index", "log"]);
const entPage = slug => byId.get(CFG.entities + "/" + slug + "/index");
const entName = slug => slug === "" ? "Internal" : (entPage(slug) ? entPage(slug).title : slug);

/* ---------------- tooltip ---------------- */
const tip = $("#tip");
function showTip(html, ev) {
  tip.innerHTML = html; tip.hidden = false;
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + w > innerWidth - 8) x = ev.clientX - w - pad;
  if (y + h > innerHeight - 8) y = ev.clientY - h - pad;
  tip.style.left = Math.max(8, x) + "px"; tip.style.top = Math.max(8, y) + "px";
}
function hideTip() { tip.hidden = true; }
function nodeTip(n) {
  const bits = [typeLabel(n.t)];
  if (n.status) bits.push(n.status);
  if (n.ts) bits.push("updated " + n.ts);
  if (n.ticket) bits.push(n.ticket);
  if (isCase(n)) bits.push(n.ent ? entName(n.ent) : (n.area || "internal"));
  return '<div class="t1">' + esc(n.title) + '</div><div class="t2">' + esc(bits.join(" · ")) + "</div>";
}

/* ---------------- header ---------------- */
const cases = nodes.filter(isCase);
const openCases = cases.filter(n => !isDone(n.status));
const ents = nodes.filter(n => n.t === CFG.entity);
const pbs = nodes.filter(n => n.t === CFG.playbook);
$("#counts").innerHTML = "<b>" + openCases.length + "</b> open " + esc(CFG.cases) + " · <b>" + ents.length + "</b> " + esc(CFG.entities) +
  " · <b>" + pbs.length + "</b> " + esc(CFG.playbooks);
$("#tab-customers").textContent = cap(CFG.entities);
$("#tab-reuse").textContent = cap(CFG.playbook) + " reuse";

/* ---------------- theme ---------------- */
const THEMES = ["system", "light", "dark"];
let theme = "system";
try { theme = localStorage.getItem("kbmap-theme") || "system"; } catch (e) {}
function applyTheme() {
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  $("#theme").textContent = "Theme: " + theme;
}
$("#theme").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % 3];
  try { localStorage.setItem("kbmap-theme", theme); } catch (e) {}
  applyTheme();
});

/* ---------------- views + routing ---------------- */
const VIEWS = ["customers", "reuse", "related", "overview"];
let current = "customers";
const drawn = {};
function show(view) {
  if (!VIEWS.includes(view)) view = "customers";
  current = view;
  document.querySelectorAll("nav.tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.view === view)));
  VIEWS.forEach(v => { $("#v-" + v).hidden = v !== view; });
  if (view === "related") drawRelated();
  if (view === "overview" && !drawn.overview) { drawOverview(); drawn.overview = true; }
  hideTip();
}
document.querySelectorAll("nav.tabs button").forEach(b => b.addEventListener("click", () => {
  show(b.dataset.view);
  try { history.replaceState(null, "", "#" + b.dataset.view); } catch (e) {}
}));

/* ---------------- customers ---------------- */
function chip(n) {
  const done = isDone(n.status);
  return '<button type="button" class="chip' + (done ? " done" : "") + '" style="--c:' + stVar(n.status) + '" data-id="' + esc(n.id) + '">' +
    '<i></i><span class="txt">' + esc(n.short) + "</span></button>";
}
function renderLegend() {
  const counts = {};
  cases.forEach(n => { const k = stKey(n.status); counts[k] = (counts[k] || 0) + 1; });
  const keys = STATUSES.concat(["other"]).filter(k => counts[k]);
  $("#legend").innerHTML = keys.map(k => '<span class="lg" style="--c:var(--st-' + k + ')"><i></i>' + (k === "other" ? "no status" : k) + " <b>" + counts[k] + "</b></span>").join("");
}
function renderCustomers() {
  const showDone = $("#show-done").checked;
  const groups = new Map();
  ents.forEach(e => groups.set(e.ent, { slug: e.ent, page: e, items: [] }));
  cases.forEach(c => {
    const k = c.ent || "";
    if (!groups.has(k)) groups.set(k, { slug: k, page: k ? null : null, items: [] });
    groups.get(k).items.push(c);
  });
  const subOf = g => {
    if (g.slug === "") return "Product and tooling, no " + CFG.entity;
    const types = new Set();
    if (g.page && g.page.dtype) g.page.dtype.split(",").forEach(s => s.trim() && types.add(s.trim()));
    nodes.forEach(n => { if (n.t === CFG.context && n.ent === g.slug && n.dtype) types.add(n.dtype); });
    return Array.from(types).join(", ");
  };
  const list = Array.from(groups.values()).map(g => {
    g.open = g.items.filter(n => !isDone(n.status)).length;
    g.done = g.items.length - g.open;
    g.name = entName(g.slug);
    return g;
  }).sort((a, b) => b.open - a.open || b.items.length - a.items.length || a.name.localeCompare(b.name));
  const shown = list.filter(g => g.open > 0 || (showDone && g.items.length));
  const quiet = list.filter(g => !shown.includes(g));
  const byTs = (a, b) => (b.ts || "").localeCompare(a.ts || "");
  $("#cust-rows").innerHTML = shown.map(g => {
    let items = g.items.filter(n => showDone || !isDone(n.status)).sort((a, b) => (isDone(a.status) - isDone(b.status)) || byTs(a, b));
    let body;
    if (g.slug === "") {
      const areas = new Map();
      items.forEach(n => { const a = n.area || "other"; if (!areas.has(a)) areas.set(a, []); areas.get(a).push(n); });
      body = Array.from(areas.entries()).sort((a, b) => a[0].localeCompare(b[0])).map(([a, xs]) => '<div class="area">' + esc(a) + "</div>" + xs.map(chip).join("")).join("");
    } else body = items.map(chip).join("");
    const cnt = "<b>" + g.open + "</b> open" + (g.done ? " · " + g.done + " resolved" : "");
    const nm = g.page ? '<button type="button" class="nm" data-id="' + esc(g.page.id) + '">' + esc(g.name) + "</button>" : '<span class="nm">' + esc(g.name) + "</span>";
    return '<div class="row"><div class="rh">' + nm + '<span class="sub">' + esc(subOf(g)) + '</span><span class="cnt">' + cnt + '</span></div><div class="chips">' + body + "</div></div>";
  }).join("");
  $("#cust-quiet").innerHTML = quiet.length ? "No open " + esc(CFG.cases) + ": " + quiet.map(g => g.page ? '<button type="button" data-id="' + esc(g.page.id) + '">' + esc(g.name) + "</button>" : esc(g.name)).join(", ") : "";
}
$("#show-done").addEventListener("change", renderCustomers);
$("#show-done-lbl").textContent = "Show resolved " + CFG.cases;

/* ---------------- reuse ---------------- */
function renderReuse() {
  const rows = pbs.map(p => {
    const iss = Array.from(adj.get(p.id)).map(id => byId.get(id)).filter(isCase);
    const custs = new Set(iss.map(i => i.ent || ""));
    return { p, iss, custs };
  });
  const used = rows.filter(r => r.iss.length).sort((a, b) => b.iss.length - a.iss.length || b.custs.size - a.custs.size || a.p.title.localeCompare(b.p.title));
  const never = rows.filter(r => !r.iss.length).sort((a, b) => a.p.title.localeCompare(b.p.title));
  const top = used[0];
  $("#reuse-lead").innerHTML = "<b>" + used.length + " of " + pbs.length + "</b> " + esc(CFG.playbooks) + " are linked from at least one " + esc(CFG.case) + " page." +
    (top ? " The most reused, <b>" + esc(top.p.short) + "</b>, covers " + plural(top.iss.length, CFG.case, CFG.cases) + " across " + plural(top.custs.size, CFG.entity, CFG.entities) + "." : "");
  $("#bars-h").textContent = "Most reused " + CFG.playbooks;
  const max = top ? top.iss.length : 1;
  $("#bars").innerHTML = used.map(r => {
    const w = Math.max(4, Math.round(r.iss.length / max * 100));
    return '<div class="brow"><button type="button" class="lbl" data-id="' + esc(r.p.id) + '" title="' + esc(r.p.title) + '">' + esc(r.p.short) + "</button>" +
      '<div class="track"><span class="barbox"><span class="fill" style="width:' + w + '%"></span></span><span class="val"><b>' + r.iss.length + "</b> " + esc(r.iss.length === 1 ? CFG.case : CFG.cases) + " · " + plural(r.custs.size, CFG.entity, CFG.entities) + "</span></div></div>";
  }).join("");
  // matrix
  $("#matrix-h").textContent = "Where each " + CFG.playbook + " was used";
  const colCount = {};
  used.forEach(r => r.iss.forEach(i => { const k = i.ent || ""; colCount[k] = (colCount[k] || 0) + 1; }));
  const cols = Object.keys(colCount).sort((a, b) => colCount[b] - colCount[a] || entName(a).localeCompare(entName(b)));
  let html = "<thead><tr><th></th>" + cols.map(c => "<th><span>" + esc(entName(c)) + "</span></th>").join("") + "</tr></thead><tbody>";
  used.forEach(r => {
    html += '<tr><th><button type="button" class="linkish" data-id="' + esc(r.p.id) + '" title="' + esc(r.p.title) + '">' + esc(r.p.short) + "</button></th>";
    cols.forEach(c => {
      const hits = r.iss.filter(i => (i.ent || "") === c);
      if (!hits.length) { html += "<td></td>"; return; }
      const pct = hits.length >= 3 ? 75 : hits.length === 2 ? 50 : 28;
      html += '<td class="on" style="--p:' + pct + '%" data-p="' + esc(r.p.id) + '" data-c="' + esc(c) + '">' + hits.length + "</td>";
    });
    html += "</tr>";
  });
  $("#matrix").innerHTML = html + "</tbody>";
  $("#matrix").onmousemove = ev => {
    const td = ev.target.closest("td.on");
    if (!td) { hideTip(); return; }
    const p = byId.get(td.dataset.p), c = td.dataset.c;
    const hits = Array.from(adj.get(p.id)).map(id => byId.get(id)).filter(n => isCase(n) && (n.ent || "") === c);
    showTip('<div class="t1">' + esc(p.short) + " · " + esc(entName(c)) + '</div><div class="t2">' + hits.map(h => "• " + esc(h.short) + " (" + esc(h.status || "no status") + ")").join("<br>") + "</div>", ev);
  };
  $("#matrix").onmouseleave = hideTip;
  const nv = $("#never");
  nv.querySelector("summary").textContent = "Never linked from any " + CFG.case + " (" + never.length + ")";
  nv.querySelector("ul").innerHTML = never.map(r => '<li><button type="button" data-id="' + esc(r.p.id) + '">' + esc(r.p.short) + "</button></li>").join("");
}

/* ---------------- related (ego graph) ---------------- */
let focusId = null, depth = 1;
function egoSet(id, d, withMeta) {
  const keep = new Map([[id, 0]]);
  let frontier = [id];
  for (let k = 1; k <= d; k++) {
    const next = [];
    frontier.forEach(f => adj.get(f).forEach(m => {
      if (keep.has(m)) return;
      const n = byId.get(m);
      if (!withMeta && META.has(n.t)) return;
      keep.set(m, k); next.push(m);
    }));
    frontier = next;
  }
  return keep;
}
function usefulDegree(id) {
  let c = 0; adj.get(id).forEach(m => { if (!META.has(byId.get(m).t)) c++; }); return c;
}
function trunc(s, n) { return s.length > n ? s.slice(0, n - 1).trimEnd() + "..." : s; }
function setFocus(id) {
  if (!byId.has(id)) return;
  focusId = id;
  if (current === "related") drawRelated(); else show("related");
}
function drawRelated() {
  const svgEl = $("#rel-svg"), empty = $("#rel-empty");
  const withMeta = $("#rel-meta").checked;
  if (!focusId) {
    svgEl.setAttribute("hidden", ""); empty.hidden = false;
    $("#rel-title").textContent = "nothing selected"; $("#rel-note").textContent = "";
    const starts = cases.slice().sort((a, b) => usefulDegree(b.id) - usefulDegree(a.id)).slice(0, 10);
    empty.innerHTML = "<p>Search for a page, or pick one of the most connected " + esc(CFG.cases) + " below, to see what it connects to.</p><ul class=\"starts\">" +
      starts.map(n => '<li><button type="button" class="linkish" data-focus="' + esc(n.id) + '"><span>' + esc(n.short) + "</span><small>" + usefulDegree(n.id) + " links</small></button></li>").join("") + "</ul>";
    return;
  }
  empty.hidden = true; svgEl.removeAttribute("hidden");
  const f = byId.get(focusId);
  $("#rel-title").textContent = f.short;
  let keep = egoSet(focusId, depth, withMeta);
  let note = "";
  const CAP = 70;
  if (keep.size > CAP) {
    const ranked = Array.from(keep.entries()).sort((a, b) => a[1] - b[1] || usefulDegree(b[0]) - usefulDegree(a[0]));
    keep = new Map(ranked.slice(0, CAP));
    note = "Showing the " + CAP + " closest of " + ranked.length + " pages.";
  }
  $("#rel-note").textContent = note || plural(keep.size - 1, "connected page", "connected pages");
  const ns = Array.from(keep.keys()).map(id => Object.assign({ hop: keep.get(id) }, byId.get(id)));
  const ls = DATA.links.filter(l => keep.has(l.source) && keep.has(l.target)).map(l => ({ source: l.source, target: l.target }));
  const W = svgEl.parentNode.clientWidth || 900;
  const H = Math.max(480, Math.min(820, innerHeight - 190));
  const svg = d3.select(svgEl).attr("viewBox", [0, 0, W, H]).attr("height", H);
  svg.selectAll("*").remove();
  const g = svg.append("g");
  svg.call(d3.zoom().scaleExtent([0.3, 4]).on("zoom", e => g.attr("transform", e.transform)));
  // Radial layout: the focus in the middle, one ring per hop, ring members grouped by type.
  const cx = W / 2, cy = H / 2, m = Math.min(W, H);
  const maxHop = Math.max(...ns.map(n => n.hop));
  const ringR = h => h === 0 ? 0 : (maxHop === 1 ? m * 0.33 : (h === 1 ? m * 0.22 : m * 0.44));
  const rings = {};
  ns.forEach(n => { (rings[n.hop] = rings[n.hop] || []).push(n); });
  Object.keys(rings).forEach(h => {
    const arr = rings[h].sort((a, b) => TYPE_ORDER.indexOf(a.t) - TYPE_ORDER.indexOf(b.t) || a.short.localeCompare(b.short));
    arr.forEach((n, i) => {
      const ang = -Math.PI / 2 + (2 * Math.PI * i) / arr.length;
      n.x = cx + ringR(+h) * Math.cos(ang) * (W > H ? 1.35 : 1); n.y = cy + ringR(+h) * Math.sin(ang);
    });
  });
  const fNode = ns.find(n => n.id === focusId); fNode.fx = cx; fNode.fy = cy;
  const sim = d3.forceSimulation(ns)
    .force("collide", d3.forceCollide(d => d.hop === 0 ? 40 : 22))
    .force("x", d3.forceX(d => d.x).strength(0.5)).force("y", d3.forceY(d => d.y).strength(0.5))
    .stop();
  for (let i = 0; i < 120; i++) sim.tick();
  const byKey = new Map(ns.map(n => [n.id, n]));
  ls.forEach(l => { l.source = byKey.get(l.source); l.target = byKey.get(l.target); });
  const labelled = n => n.hop <= 1 || rings[n.hop].length <= 24;
  g.append("g").selectAll("line").data(ls).join("line").attr("class", d => "edge" + (d.source.hop === 0 || d.target.hop === 0 ? " hot" : ""))
    .attr("x1", d => d.source.x).attr("y1", d => d.source.y).attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  const r = n => n.id === focusId ? 15 : 7 + Math.min(Math.sqrt(usefulDegree(n.id)), 4) * 1.4;
  const node = g.append("g").selectAll("g").data(ns).join("g").attr("class", "node").attr("transform", d => "translate(" + d.x + "," + d.y + ")");
  node.append("circle").attr("r", r).style("fill", d => typeVar(d.t))
    .style("stroke", d => isCase(d) ? stVar(d.status) : "var(--surface)").style("stroke-width", d => isCase(d) ? 3.5 : 1.5);
  node.filter(labelled).append("text").attr("class", d => "nlabel" + (d.id === focusId ? " focus" : ""))
    .attr("text-anchor", d => d.hop === 0 ? "middle" : (d.x >= cx ? "start" : "end"))
    .attr("x", d => d.hop === 0 ? 0 : (d.x >= cx ? r(d) + 6 : -r(d) - 6))
    .attr("y", d => d.hop === 0 ? r(d) + 18 : 4)
    .text(d => trunc(d.short, d.id === focusId ? 60 : 34));
  node.on("mousemove", (ev, d) => showTip(nodeTip(d), ev)).on("mouseleave", hideTip)
    .on("click", (ev, d) => { hideTip(); openReader(d.id, true); if (d.id !== focusId) { focusId = d.id; drawRelated(); } });
}
document.querySelectorAll(".seg button").forEach(b => b.addEventListener("click", () => {
  depth = +b.dataset.depth;
  document.querySelectorAll(".seg button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  drawRelated();
}));
$("#rel-meta").addEventListener("change", drawRelated);

/* ---------------- overview ---------------- */
const ovOn = new Set(TYPE_ORDER.filter(t => !META.has(t)));
let ovOrphans = false, ovSel = null;
function drawOverview() {
  const tools = $("#ov-tools");
  const typeCount = {}; nodes.forEach(n => { typeCount[n.t] = (typeCount[n.t] || 0) + 1; });
  tools.innerHTML = TYPE_ORDER.filter(t => typeCount[t]).map(t =>
    '<label><input type="checkbox" data-type="' + esc(t) + '"' + (ovOn.has(t) ? " checked" : "") + '> <span class="lg" style="--c:' + typeVar(t) + '"><i></i>' + esc(typeLabel(t)) + " <b>" + typeCount[t] + "</b></span></label>").join("") +
    '<label><input type="checkbox" id="ov-orph"' + (ovOrphans ? " checked" : "") + "> Show unlinked pages</label>" + '<span class="note" id="ov-note"></span>';
  tools.querySelectorAll("input[data-type]").forEach(i => i.addEventListener("change", () => { i.checked ? ovOn.add(i.dataset.type) : ovOn.delete(i.dataset.type); drawOverview(); }));
  $("#ov-orph").addEventListener("change", e => { ovOrphans = e.target.checked; drawOverview(); });

  const vis = nodes.filter(n => ovOn.has(n.t));
  const visSet = new Set(vis.map(n => n.id));
  const ls = DATA.links.filter(l => visSet.has(l.source) && visSet.has(l.target)).map(l => ({ source: l.source, target: l.target }));
  const hiddenLinks = DATA.links.filter(l => visSet.has(l.source) !== visSet.has(l.target)).length;
  const linked = new Set(); ls.forEach(l => { linked.add(l.source); linked.add(l.target); });
  const ns = vis.filter(n => ovOrphans || linked.has(n.id)).map(n => Object.assign({}, n));
  const orphanCount = vis.length - vis.filter(n => linked.has(n.id)).length;
  const deg = {}; ls.forEach(l => { deg[l.source] = (deg[l.source] || 0) + 1; deg[l.target] = (deg[l.target] || 0) + 1; });
  $("#ov-note").textContent = [hiddenLinks ? plural(hiddenLinks, "link goes", "links go") + " to hidden types" : "", !ovOrphans && orphanCount ? plural(orphanCount, "unlinked page", "unlinked pages") + " hidden" : ""].filter(Boolean).join(" · ");

  const svgEl = $("#ov-svg");
  const W = svgEl.parentNode.clientWidth || 1000;
  const H = Math.max(520, Math.min(900, innerHeight - 170));
  const svg = d3.select(svgEl).attr("viewBox", [0, 0, W, H]).attr("height", H);
  svg.selectAll("*").remove();
  const g = svg.append("g");
  const zoom = d3.zoom().scaleExtent([0.2, 6]).on("zoom", e => g.attr("transform", e.transform));
  svg.call(zoom);
  const byN = new Map(ns.map(n => [n.id, n]));
  const anchorOf = n => (n.t === CFG.case || n.t === CFG.context) && n.ent ? byN.get(CFG.entities + "/" + n.ent + "/index") : null;
  function cluster(alpha) {
    ns.forEach(n => { const a = anchorOf(n); if (a) { n.vx += (a.x - n.x) * 0.08 * alpha; n.vy += (a.y - n.y) * 0.08 * alpha; } });
  }
  const sim = d3.forceSimulation(ns)
    .force("link", d3.forceLink(ls.filter(l => byN.has(l.source) && byN.has(l.target))).id(d => d.id).distance(45).strength(0.25))
    .force("charge", d3.forceManyBody().strength(-90))
    .force("collide", d3.forceCollide(d => 6 + Math.sqrt(deg[d.id] || 0) * 2))
    .force("cluster", cluster)
    .force("x", d3.forceX(W / 2).strength(0.04)).force("y", d3.forceY(H / 2).strength(0.06))
    .stop();
  for (let i = 0; i < 400; i++) sim.tick();
  const linkSel = g.append("g").selectAll("line").data(sim.force("link").links()).join("line").attr("class", "edge")
    .attr("x1", d => d.source.x).attr("y1", d => d.source.y).attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  const rad = n => Math.max(3.5, Math.min(16, 3 + Math.sqrt(deg[n.id] || 0) * 2.2));
  const node = g.append("g").selectAll("g").data(ns).join("g").attr("class", "node").attr("transform", d => "translate(" + d.x + "," + d.y + ")");
  node.append("circle").attr("r", rad).style("fill", d => typeVar(d.t))
    .style("stroke", d => isCase(d) ? stVar(d.status) : "var(--surface)").style("stroke-width", d => isCase(d) ? 2 : 1)
    .style("opacity", d => isCase(d) && isDone(d.status) ? 0.45 : 1);
  const label = node.append("text").attr("class", "nlabel").attr("text-anchor", "middle").attr("y", d => -rad(d) - 5)
    .text(d => trunc(d.short, 34)).style("display", d => d.t === CFG.entity ? null : "none");
  function highlight(id) {
    ovSel = id;
    const nb = id ? new Set([id].concat(Array.from(adj.get(id)))) : null;
    node.classed("dim", d => nb && !nb.has(d.id));
    label.style("display", d => (d.t === CFG.entity && !nb) || (nb && nb.has(d.id)) ? null : "none");
    linkSel.classed("hot", d => nb && (d.source.id === id || d.target.id === id)).classed("dim", d => nb && d.source.id !== id && d.target.id !== id);
  }
  node.on("mousemove", (ev, d) => showTip(nodeTip(d), ev)).on("mouseleave", hideTip)
    .on("click", (ev, d) => { ev.stopPropagation(); hideTip(); highlight(d.id); openReader(d.id, true); });
  svg.on("click", () => highlight(null));
  // zoom to fit
  if (ns.length) {
    const xs = ns.map(n => n.x), ys = ns.map(n => n.y);
    const x0 = Math.min(...xs) - 40, x1 = Math.max(...xs) + 40, y0 = Math.min(...ys) - 40, y1 = Math.max(...ys) + 40;
    const k = Math.min(2, 0.95 * Math.min(W / (x1 - x0), H / (y1 - y0)));
    svg.call(zoom.transform, d3.zoomIdentity.translate(W / 2 - k * (x0 + x1) / 2, H / 2 - k * (y0 + y1) / 2).scale(k));
  }
  if (ovSel && byN.has(ovSel)) highlight(ovSel);
}

/* ---------------- reader ---------------- */
const reader = $("#reader"), rdBody = $("#rd-body"), rdBack = $("#rd-back");
const hist = []; let readerId = null;
function openReader(id, push) {
  const n = byId.get(id); if (!n) return;
  if (push && readerId && readerId !== id) hist.push(readerId);
  readerId = id;
  rdBack.disabled = !hist.length;
  $("#rd-title").textContent = n.title;
  const meta = ['<span class="badge" style="--c:' + typeVar(n.t) + '">' + esc(typeLabel(n.t)) + "</span>"];
  if (n.status) meta.push('<span class="pill" style="--c:' + stVar(n.status) + '">' + esc(n.status) + "</span>");
  if (n.ent) meta.push("<span>" + esc(entName(n.ent)) + "</span>");
  if (n.area) meta.push("<span>area: " + esc(n.area) + "</span>");
  if (n.ticket) meta.push("<span>" + esc(n.ticket) + "</span>");
  if (n.ts) meta.push("<span>updated " + esc(n.ts) + "</span>");
  meta.push('<span><code>' + esc(n.id) + ".md</code></span>");
  $("#rd-meta").innerHTML = meta.join("");
  rdBody.innerHTML = renderBody(n.body);
  rdBody.querySelectorAll("a[href]").forEach(a => {
    const h = a.getAttribute("href");
    if (h.startsWith("#n:")) {
      const t = decodeURIComponent(h.slice(3));
      a.classList.add(byId.has(t) ? "kb" : "missing");
      if (!byId.has(t)) a.title = "page not found: " + t;
    } else if (h.startsWith("#x:")) { a.classList.add("missing"); a.title = "unresolved link: " + decodeURIComponent(h.slice(3)); }
    else if (/^https?:/.test(h)) { a.target = "_blank"; a.rel = "noopener"; }
  });
  rdBody.scrollTop = 0;
  reader.hidden = false;
}
/* Page bodies can come from captured email or chat text, so they are untrusted HTML: render with
   marked, then sanitize with DOMPurify. No DOMPurify (offline, CDN blocked) means plain text. */
const SAFE_URI = /^(?:https?:|#|\.{0,2}\/|[^:/?#]+(?:[/?#]|$))/i;
function renderBody(md) {
  if (typeof DOMPurify === "undefined" || typeof marked === "undefined") return "<pre>" + esc(md) + "</pre>";
  let raw;
  try { raw = marked.parse(md, { gfm: true, breaks: false }); }
  catch (e) { return "<pre>" + esc(md) + "</pre>"; }
  return DOMPurify.sanitize(raw, {
    ALLOWED_URI_REGEXP: SAFE_URI,
    FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select", "iframe", "frame", "object", "embed", "link", "meta", "base", "svg", "math"],
    FORBID_ATTR: ["style", "srcset", "ping", "action", "formaction"],
  });
}
function closeReader() { reader.hidden = true; hist.length = 0; readerId = null; }
rdBody.addEventListener("click", e => {
  const a = e.target.closest("a[href^='#n:'], a[href^='#x:']");
  if (!a) return;
  e.preventDefault();
  const h = a.getAttribute("href");
  if (h.startsWith("#n:")) openReader(decodeURIComponent(h.slice(3)), true);
});
rdBack.addEventListener("click", () => { if (!hist.length) return; const p = hist.pop(); readerId = null; openReader(p, false); rdBack.disabled = !hist.length; });
$("#rd-close").addEventListener("click", closeReader);
$("#rd-rel").addEventListener("click", () => { if (readerId) setFocus(readerId); });
document.addEventListener("keydown", e => { if (e.key === "Escape") { if (!$("#results").hidden) closeResults(); else closeReader(); } });

/* delegated clicks on any [data-id] (chips, names, bars) and [data-focus] */
document.addEventListener("click", e => {
  const f = e.target.closest("[data-focus]");
  if (f) { setFocus(f.dataset.focus); openReader(f.dataset.focus, true); return; }
  const el = e.target.closest("main [data-id]");
  if (el) { hideTip(); openReader(el.dataset.id, true); }
});
document.addEventListener("mousemove", e => {
  const el = e.target.closest(".chip, .brow .lbl, .linkish[data-id]");
  if (el && el.dataset.id) showTip(nodeTip(byId.get(el.dataset.id)), e);
  else if (!e.target.closest("svg, td.on")) hideTip();
});

/* ---------------- search ---------------- */
const q = $("#q"), results = $("#results");
let hits = [], active = -1;
function closeResults() { results.hidden = true; active = -1; }
function runSearch() {
  const s = q.value.trim().toLowerCase();
  if (!s) { closeResults(); return; }
  const terms = s.split(/\s+/);
  hits = nodes.map(n => {
    const hay = (n.title + " " + n.id).toLowerCase();
    if (!terms.every(t => hay.includes(t))) return null;
    let score = n.title.toLowerCase().startsWith(s) ? 0 : n.title.toLowerCase().includes(s) ? 1 : 2;
    if (META.has(n.t)) score += 3;
    return { n, score };
  }).filter(Boolean).sort((a, b) => a.score - b.score || a.n.title.localeCompare(b.n.title)).slice(0, 12).map(x => x.n);
  active = hits.length ? 0 : -1;
  results.innerHTML = hits.length ? hits.map((n, i) => '<button type="button" class="res' + (i === active ? " active" : "") + '" data-i="' + i + '"><span class="lg" style="--c:' + typeVar(n.t) + '"><i></i></span><span class="tt">' + esc(n.title) + '</span><span class="ty">' + esc(typeLabel(n.t)) + "</span></button>").join("")
    : '<div class="note" style="padding:8px">No pages match.</div>';
  results.hidden = false;
}
function pick(i) {
  const n = hits[i]; if (!n) return;
  closeResults(); q.blur();
  setFocus(n.id); openReader(n.id, true);
  try { history.replaceState(null, "", "#related"); } catch (e) {}
}
q.addEventListener("input", runSearch);
q.addEventListener("focus", () => { if (q.value.trim()) runSearch(); });
q.addEventListener("keydown", e => {
  if (results.hidden) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    active = (active + (e.key === "ArrowDown" ? 1 : -1) + hits.length) % hits.length;
    results.querySelectorAll(".res").forEach((b, i) => b.classList.toggle("active", i === active));
  } else if (e.key === "Enter") { e.preventDefault(); pick(active); }
});
results.addEventListener("click", e => { const b = e.target.closest(".res"); if (b) pick(+b.dataset.i); });
document.addEventListener("click", e => { if (!e.target.closest(".search")) closeResults(); });

/* ---------------- footer: data gaps ---------------- */
(function () {
  const dTargets = {}; DATA.dangling.forEach(d => { (dTargets[d.target] = dTargets[d.target] || []).push(d.source); });
  const uTargets = {}; DATA.unresolved.forEach(d => { (uTargets[d.target] = uTargets[d.target] || []).push(d.source); });
  const li = (obj, wrap) => Object.keys(obj).sort((a, b) => obj[b].length - obj[a].length || a.localeCompare(b)).map(t =>
    "<li><code>" + esc(wrap(t)) + "</code> from " + obj[t].slice(0, 3).map(s => '<button type="button" class="linkish" data-open="' + esc(s) + '">' + esc((byId.get(s) || { short: s }).short) + "</button>").join(", ") + (obj[t].length > 3 ? " and " + (obj[t].length - 3) + " more" : "") + "</li>").join("");
  const noPb = DATA.noPlaybook.map(id => byId.get(id)).filter(Boolean);
  $("#foot").innerHTML = "<div>Generated " + esc(DATA.generated) + " · " + nodes.length + " pages · " + DATA.links.length + " links</div>" +
    "<details><summary>Data gaps: <b>" + noPb.length + "</b> " + esc(CFG.cases) + " link no " + esc(CFG.playbook) + " · <b>" + DATA.dangling.length + "</b> links point at missing pages · <b>" + DATA.unresolved.length + "</b> unresolved [[wiki-links]]</summary>" +
    "<p>" + esc(cap(CFG.cases)) + " with no " + esc(CFG.playbook) + ":</p><ul>" + noPb.map(n => '<li><button type="button" class="linkish" data-open="' + esc(n.id) + '">' + esc(n.short) + "</button></li>").join("") + "</ul>" +
    "<p>Links to missing pages:</p><ul>" + li(dTargets, t => t + ".md") + "</ul>" +
    "<p>Unresolved [[wiki-links]]:</p><ul>" + li(uTargets, t => "[[" + t + "]]") + "</ul></details>";
  $("#foot").addEventListener("click", e => { const b = e.target.closest("[data-open]"); if (b) openReader(b.dataset.open, true); });
})();

/* ---------------- boot ---------------- */
applyTheme();
renderLegend();
renderCustomers();
renderReuse();
(function boot() {
  const tokens = decodeURIComponent((location.hash || "").slice(1)).split(",").filter(Boolean);
  let view = "customers";
  tokens.forEach(t => {
    if (VIEWS.includes(t)) view = t;
    else if (t === "dark" || t === "light") { theme = t; applyTheme(); }
    else if (t.startsWith("focus=")) { focusId = byId.has(t.slice(6)) ? t.slice(6) : null; }
    else if (t === "showdone") { $("#show-done").checked = true; renderCustomers(); }
  });
  show(view);
})();
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (current === "related") drawRelated(); if (current === "overview") drawOverview(); }, 200);
});
</script>
</body>
</html>
"""


def main():
    _cfgmod.require_kb()
    output = KB / "graph.html"
    if len(sys.argv) > 2 and sys.argv[1] == "-o":
        output = Path(sys.argv[2])
    render(output)


if __name__ == "__main__":
    main()
