#!/usr/bin/env python3
"""
leyden new: scaffold one entry from the rendered templates in _templates/.

    leyden new entity <slug>                      -> <entities>/<slug>/index.md + log.md + <contexts>/ + <cases>/
    leyden new context <entity-slug> <slug>       -> <entities>/<entity>/<contexts>/<slug>.md
    leyden new case <entity-slug> <slug>          -> <entities>/<entity>/<cases>/<slug>.md
    leyden new case internal <slug>               -> internal/<cases>/<slug>.md
    leyden new playbook <slug>                    -> <playbooks>/<slug>.md
    leyden new project <slug>                     -> projects/<slug>.md

The slot words are the GENERIC ones (entity/context/case/playbook) so the command reads the same
for every team; the configured names (account/environment/... or use-case/blocker/...) are also
accepted. Never overwrites: an existing file is reported and left alone.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

KB = _config.KB   # resolved KB (tools/_config.py: LEYDEN_HOME, .kb-path, legacy)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def die(msg: str) -> None:
    sys.exit(f"leyden new: {msg}")


def stamp(text: str, slug: str, title: str, T: dict, extra: dict | None = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    author = _config.get("engineer.slug", "me")
    text = re.sub(r"^timestamp:.*$", f"timestamp: {now}", text, flags=re.M)
    text = re.sub(r"^author:.*$", f"author: {author}", text, flags=re.M)
    text = re.sub(r"^title:.*$", f"title: {title}", text, flags=re.M)
    for k, val in (extra or {}).items():
        text = re.sub(rf"^{re.escape(k)}:.*$", f"{k}: {val}", text, flags=re.M)
    return text


def write(path: Path, text: str) -> None:
    kb = KB.resolve()
    if path.resolve() != kb and kb not in path.resolve().parents:   # never write outside the KB
        die(f"refused: {path} resolves outside the KB")
    if path.exists():
        print(f"kept   {path.relative_to(KB)} (already exists)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"wrote  {path.relative_to(KB)}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    _config.require_kb()
    T = _config.taxonomy()
    D = _config.dirs()
    aliases = {T[s]["singular"]: s for s in _config.SLOTS}
    slot = aliases.get(argv[0], argv[0])
    tpl = KB / "_templates"
    if not tpl.exists():
        die("no _templates/ yet: run `leyden init` first")

    def load(slot_name: str) -> str:
        p = tpl / f"{T[slot_name]['singular']}.md"
        if not p.exists():
            die(f"missing template {p.relative_to(KB)}: rerun `leyden init`")
        return p.read_text(encoding="utf-8")

    if slot == "entity":
        slug = argv[1]
        SLUG_RE.match(slug) or die(f"bad slug '{slug}'")
        base = KB / D["entity"] / slug
        title = slug.replace("-", " ").title()
        write(base / "index.md", stamp(load("entity"), slug, title, T))
        log = (tpl / "log.md").read_text(encoding="utf-8") if (tpl / "log.md").exists() else "# worklog\n"
        ent = T['entity']['singular']
        log = (log.replace(f"<{ent}-slug>", slug).replace(f"<{ent.capitalize()}>", title)
               .replace(f"<{ent}>", title))
        write(base / "log.md", stamp(log, slug, f"{title} worklog", T))
        for sub in (D["context"], D["case"]):
            (base / sub).mkdir(parents=True, exist_ok=True)
            (base / sub / ".gitkeep").touch()
        return 0

    if slot in ("context", "case"):
        if len(argv) < 3:
            die(f"usage: leyden new {argv[0]} <{T['entity']['singular']}-slug|internal> <slug>")
        owner, slug = argv[1], argv[2]
        SLUG_RE.match(slug) or die(f"bad slug '{slug}'")
        text = load(slot)
        title = slug.replace("-", " ")
        if owner == "internal":
            if slot != "case":
                die("only cases can be internal")
            # An internal case has no entity, so no context under one either.
            for key in (T["entity"]["singular"], T["context"]["singular"]):
                text = re.sub(rf"^{re.escape(key)}:.*\n", "", text, flags=re.M)
            write(KB / D["internal"] / D["case"] / f"{slug}.md", stamp(text, slug, title, T))
            return 0
        # The owner is a path segment: "../.." passed is_dir() and wrote outside the KB.
        SLUG_RE.fullmatch(owner) or die(f"bad {T['entity']['singular']} slug '{owner}'")
        if not (KB / D["entity"] / owner).is_dir():
            die(f"no such {T['entity']['singular']}: {owner} (create it with `leyden new entity {owner}`)")
        text = re.sub(r"^area:.*\n", "", text, flags=re.M)
        write(KB / D["entity"] / owner / D[slot] / f"{slug}.md",
              stamp(text, slug, title, T, {T["entity"]["singular"]: owner}))
        return 0

    if slot == "playbook":
        slug = argv[1]
        SLUG_RE.match(slug) or die(f"bad slug '{slug}'")
        write(KB / D["playbook"] / f"{slug}.md", stamp(load("playbook"), slug, slug.replace("-", " "), T))
        return 0

    if slot == "project":
        slug = argv[1]
        SLUG_RE.match(slug) or die(f"bad slug '{slug}'")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        text = (f"---\ntype: project\nauthor: {_config.get('engineer.slug', 'me')}\ntitle: {slug.replace('-', ' ')}\n"
                f"description: <one line>\ntags: []\nstatus: active\ntimestamp: {now}\n---\n\n# {slug.replace('-', ' ')}\n\n"
                "## Goal\n\n## State\n\n## Next\n")
        write(KB / D["projects"] / f"{slug}.md", text)
        return 0

    die(f"unknown slot '{argv[0]}' (want entity|context|case|playbook|project, or your configured names)")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
