#!/usr/bin/env python3
"""
leyden export: a handover bundle for one customer (entity), for when someone else takes over this
work or the full history is needed outside your KB.

    leyden export --customer acme                    # -> ./acme-handover-<date>/
    leyden export --customer acme --out ~/handover    # explicit folder (must be outside the KB)

The bundle holds copies, never moves: the entity's profile and log, every context (deployment),
every case (open and resolved), and every playbook (runbook) a case links to. Secrets are removed
from the COPIES with the same redactor the commit guard uses, then scanned again; anything still
flagged is listed so a person can check it before sending. HANDOVER.md on top is the one page to
read first: open cases, resolved cases, contexts, linked playbooks, recent log headings.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import _config as C  # noqa: E402

TERMINAL = {"resolved", "discarded", "closed", "done", "wontfix"}
FM_RE = re.compile(r"^---\n(.*?)\n---\n?", re.S)
LINK_RE = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")
WIKI_RE = re.compile(r"\[\[([^\]|#]+)")


def frontmatter(text: str) -> dict:
    m = FM_RE.match(text.replace("\r\n", "\n").lstrip("﻿"))
    out = {}
    if not m:
        return out
    for ln in m.group(1).split("\n"):
        if ":" in ln and not ln.startswith((" ", "\t", "-")):
            k, v = ln.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def listish(v: str) -> list:
    return [s.strip().strip('"\'') for s in (v or "").strip("[]").split(",") if s.strip()]


def resolve_entity(name: str, D: dict) -> str | None:
    root = C.KB / D["entity"]
    # A folder is enough: capture can create <entity>/<slug>/issues/ before anyone writes the
    # profile (index.md), and that customer still needs a handover bundle.
    if name and "/" not in name and name not in (".", "..") and (root / name).is_dir():
        return name
    low = name.lower()
    try:
        reg = C.registry()
    except Exception:
        reg = {}
    for slug, info in (reg or {}).items():
        aliases = [a.lower() for a in C.entity_aliases(info)[0]] if info else []
        if low == slug.lower() or low in aliases:
            return slug if (root / slug).is_dir() else None
    return None


def main() -> int:
    C.require_kb()
    D, T = C.dirs(), C.taxonomy()
    ent_word = T["entity"]["singular"]
    ap = argparse.ArgumentParser(prog="leyden export", description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--customer", "--entity", dest="entity", required=True, help=f"the {ent_word} slug or alias")
    ap.add_argument("--out", help="output folder (default: ./<slug>-handover-<date>/, never inside the KB)")
    ap.add_argument("--force", action="store_true", help="write into an existing, non-empty folder")
    a = ap.parse_args()

    slug = resolve_entity(a.entity, D)
    if not slug:
        print(f"leyden export: no {ent_word} '{a.entity}' in {C.KB / D['entity']}/", file=sys.stderr)
        return 2
    src = C.KB / D["entity"] / slug
    out = Path(a.out).expanduser() if a.out else Path.cwd() / f"{slug}-handover-{dt.date.today()}"
    out = out.resolve()
    for forbidden, why in ((C.KB.resolve(), "the KB (it would be committed)"),
                           (C.TOOL_ROOT.resolve(), "the Leyden tool checkout")):
        if out == forbidden or forbidden in out.parents:
            print(f"leyden export: {out} is inside {why}; choose --out elsewhere", file=sys.stderr)
            return 2
    if out.exists() and any(out.iterdir()) and not a.force:
        print(f"leyden export: {out} exists and is not empty (use --force or another --out)", file=sys.stderr)
        return 2

    files = sorted(p for p in src.rglob("*.md") if p.is_file())
    cases, contexts, playbooks = [], [], set()
    pb_dir = C.KB / D["playbook"]
    pb_key = T["playbook"]["plural"]
    for f in files:
        rel = f.relative_to(src)
        text = f.read_text(encoding="utf-8", errors="replace")
        fm = frontmatter(text)
        if rel.parts[0] == D["case"]:
            cases.append((f, fm))
        elif rel.parts[0] == D["context"]:
            contexts.append((f, fm))
        for slugp in listish(fm.get(pb_key, "")):
            playbooks.add(pb_dir / f"{slugp}.md")
        for m in LINK_RE.finditer(text):
            t = (f.parent / m.group(1)).resolve()
            if pb_dir.resolve() in t.parents:
                playbooks.add(t)
        for m in WIKI_RE.finditer(text):
            playbooks.add(pb_dir / f"{m.group(1).strip()}.md")
    playbooks = sorted(p for p in playbooks if p.exists())

    out.mkdir(parents=True, exist_ok=True)
    copied = []
    for f in files:
        dst = out / D["entity"] / slug / f.relative_to(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
        copied.append(dst)
    for p in playbooks:
        dst = out / D["playbook"] / p.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        copied.append(dst)

    # Summary page
    idx = frontmatter((src / "index.md").read_text(encoding="utf-8", errors="replace")) if (src / "index.md").exists() else {}
    title = idx.get("title") or slug

    def row(f, fm):
        rel = (Path(D["entity"]) / slug / f.relative_to(src)).as_posix()
        ticket = fm.get("ticket_id") or fm.get("linear_id") or ""
        ticket = "" if ticket in ("null", '""', "''") else ticket
        cell = fm.get("title", f.stem).replace('\\"', '"').replace("|", "\\|")
        return (f"| {fm.get('status', '?')} | {cell} | {fm.get('timestamp', '')[:10]} "
                f"| {ticket} | [{f.name}]({rel}) |")

    open_c = [c for c in cases if c[1].get("status", "") not in TERMINAL]
    done_c = [c for c in cases if c[1].get("status", "") in TERMINAL]
    open_c.sort(key=lambda c: c[1].get("timestamp", ""), reverse=True)
    done_c.sort(key=lambda c: c[1].get("timestamp", ""), reverse=True)
    case_w, cases_w = T["case"]["singular"], T["case"]["plural"]
    lines = [f"# Handover: {title}", "",
             f"Exported {dt.date.today()} from the knowledge base `{C.KB.name}`. Copies only; "
             "secrets were removed by Leyden's redactor (pattern-based: check before sending outside the team).", ""]
    if idx.get("description"):
        lines += [idx["description"], ""]
    lines += [f"## Open {cases_w} ({len(open_c)})", ""]
    if open_c:
        lines += ["| Status | Title | Last updated | Ticket | File |", "|---|---|---|---|---|"]
        lines += [row(f, fm) for f, fm in open_c]
    else:
        lines.append(f"No open {cases_w}.")
    lines += ["", f"## Resolved {cases_w} ({len(done_c)})", ""]
    if done_c:
        lines += ["| Status | Title | Last updated | Ticket | File |", "|---|---|---|---|---|"]
        lines += [row(f, fm) for f, fm in done_c]
    else:
        lines.append(f"No resolved {cases_w}.")
    lines += ["", f"## {T['context']['plural'].capitalize()} ({len(contexts)})", ""]
    for f, fm in contexts:
        rel = (Path(D["entity"]) / slug / f.relative_to(src)).as_posix()
        lines.append(f"- [{fm.get('title', f.stem)}]({rel}){': ' + fm['description'] if fm.get('description') else ''}")
    lines += ["", f"## Linked {T['playbook']['plural']} ({len(playbooks)})", ""]
    for p in playbooks:
        fm = frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        lines.append(f"- [{fm.get('title', p.stem)}]({D['playbook']}/{p.name})")
    log = src / "log.md"
    if log.exists():
        heads = [ln for ln in log.read_text(encoding="utf-8", errors="replace").split("\n") if ln.startswith("## ")][:15]
        lines += ["", "## Recent log entries (newest first)", ""] + [f"- {h[3:]}" for h in heads]
        lines += ["", f"Full log: [log.md]({D['entity']}/{slug}/log.md)"]
    summary = out / "HANDOVER.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    copied.append(summary)

    # Redact the copies, then scan again: report what is still flagged.
    red = [sys.executable, "-I", str(TOOLS / "leyden-redact.py")]
    r = subprocess.run(red + ["--redact"] + [str(p) for p in copied], capture_output=True, text=True)
    if r.returncode not in (0, 1):
        print("leyden export: the redactor failed; the bundle was NOT made safe. Removing it.", file=sys.stderr)
        shutil.rmtree(out, ignore_errors=True)
        return 1
    n_red = len([ln for ln in r.stdout.split("\n") if ln.strip()])
    s = subprocess.run(red + ["--scan"] + [str(p) for p in copied], capture_output=True, text=True)
    left = [ln for ln in s.stdout.split("\n") if ln.strip() and not ln.startswith("entropy-hex")]
    print(f"leyden export: {out}")
    print(f"  {len(files)} {ent_word} files, {len(open_c)} open + {len(done_c)} resolved {cases_w}, "
          f"{len(playbooks)} linked {T['playbook']['plural']}; read HANDOVER.md first")
    print(f"  secrets removed: {n_red}")
    if left:
        print("  still flagged after redaction (check by hand before sending):")
        for ln in left[:20]:
            print("    " + ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
