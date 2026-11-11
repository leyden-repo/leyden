#!/usr/bin/env python3
"""
Leyden config + taxonomy loader. Parses `leyden.yaml` at the KB root (falls back to the legacy
`config.yaml`). stdlib only, on purpose: the file format is a deliberately MINIMAL subset of YAML
(two/three-level `key: value` with indentation scoping and inline `[a, b]` lists), documented in
leyden.yaml itself. Not a general YAML parser.

Python use:
    from _config import load, taxonomy, dirs
    cfg = load()            # nested dict, {} if file missing
    T = taxonomy()          # {"entity": {"singular": "customer", "plural": "customers"}, ...}
    D = dirs()              # {"entity": "customers", "context": "deployments", "case": "issues",
                            #  "playbook": "runbooks", "internal": "internal", "projects": "projects"}

Shell use:
    python3 tools/_config.py get engineer.slug                # value or empty, exit 0
    python3 tools/_config.py get push.interval_seconds 72000  # with default
    eval "$(python3 tools/_config.py shell)"                  # exports LDN_ENTITY, LDN_ENTITIES,
                                                              # LDN_CONTEXT(S), LDN_CASE(S),
                                                              # LDN_PLAYBOOK(S), LDN_INTERNAL, LDN_PROJECTS
    python3 tools/_config.py registry                         # "slug<TAB>alias1|alias2" per entity
    python3 tools/_config.py domains                          # every email domain, one per line
    python3 tools/_config.py presets                          # preset names, one per line

Missing file or missing key is never an error: callers always have a working default, so a clone
without leyden.yaml behaves exactly like the default `deployment` preset
(customers/deployments/issues/runbooks).

## Taxonomy

Leyden has four renamable slots. Every tool, template, frontmatter key and directory name is derived
from them, so a team can call things whatever they already call them:

    slot      what it is                                  `deployment`  `application`
    entity    who the work is for (top-level folder)      customer      customer
    context   one concrete setup under an entity          deployment    use-case
    case      one thing that went wrong or was asked      issue         blocker
    playbook  reusable, entity-agnostic fix / technique   runbook       playbook

Two more folders are fixed (they are the same for every role): `internal/` for cases with no entity
(keyed on `area:`), and `projects/` for initiatives. Slot names are set once by `leyden init`
(individual) or by the team owner (team mode, committed in leyden.yaml) and never change afterwards:
renaming a slot means renaming directories and frontmatter keys across every entry.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import os

# The tool checkout and the KB are separate repos. TOOL_ROOT is this checkout (pull-only, holds no
# data); KB is your own repo, found the same way as tools/_kb.sh does for the shell tools:
#   LEYDEN_HOME env  ->  <tool root>/.kb-path (relative to the tool's parent, or absolute)
#   ->  legacy KB inside this checkout (has leyden.yaml)
# KB_SOURCE says which one won ("env", "kb-path", "legacy"), or "none" when nothing is configured.
# With "none", KB points at a path that does not exist; tools that write call require_kb() first.
TOOL_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = TOOL_ROOT / "tools"
KB_PATH_FILE = TOOL_ROOT / ".kb-path"


def kb_path_from_text(line: str) -> Path | None:
    """The KB a .kb-path line names. A relative line is relative to the tool checkout's PARENT
    (the workspace), so moving or copying the workspace keeps each copy on its own KB; an absolute
    line (older inits, or a KB outside the workspace) is used as is."""
    line = (line or "").strip()
    if not line:
        return None
    p = Path(line).expanduser()
    return p if p.is_absolute() else TOOL_ROOT.parent / p


def kb_path_text(kb: Path) -> str:
    """What init writes into .kb-path for <kb>: relative to the tool's parent when the KB is under
    it (survives mv/cp of the workspace), else absolute."""
    kb = Path(kb).resolve()
    try:
        return kb.relative_to(TOOL_ROOT.parent.resolve()).as_posix()
    except ValueError:
        return str(kb)


def read_kb_path_file() -> Path | None:
    try:
        return kb_path_from_text(KB_PATH_FILE.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, IndexError):
        return None


def resolve_kb() -> tuple[Path | None, str]:
    env = os.environ.get("LEYDEN_HOME", "").strip()
    if env:
        p = Path(env).expanduser()
        if not p.is_dir():
            return None, "none"
        p = p.resolve()
        return p, ("legacy" if p == TOOL_ROOT else "env")
    if KB_PATH_FILE.is_file():
        p = read_kb_path_file()
        return (p.resolve(), "kb-path") if p and p.is_dir() else (None, "none")
    if (TOOL_ROOT / "leyden.yaml").is_file():
        return TOOL_ROOT, "legacy"
    return None, "none"


def set_kb(path: Path, source: str = "explicit") -> None:
    """Point this module (and so every importer reading _config.KB) at a KB folder."""
    global KB, KB_SOURCE, STATE, CONFIG_CANDIDATES, LOCAL_CONFIG
    KB = Path(path).resolve()
    KB_SOURCE = source
    STATE = KB / ".leyden"   # machine-local runtime state: logs, queue, locks, offsets, index.db
    CONFIG_CANDIDATES = (KB / "leyden.yaml", KB / "config.yaml")
    LOCAL_CONFIG = KB / "leyden.local.yaml"   # per-person overlay (gitignored) for team-mode clones


def require_kb() -> Path:
    """Exit with the standard message when no KB is configured; else return it (state dir made)."""
    if KB_SOURCE == "none":
        sys.exit("leyden: no KB configured, run: leyden init")
    STATE.mkdir(parents=True, exist_ok=True)
    return KB


_kb, _src = resolve_kb()
set_kb(_kb if _kb is not None else TOOL_ROOT / ".no-kb-configured", _src)

SLOTS = ("entity", "context", "case", "playbook")
FIXED_DIRS = {"internal": "internal", "projects": "projects"}

# Role presets. `leyden init` offers these; `custom` asks for every slot.
PRESETS = {
    "deployment": {
        "label": "Deployments (customer > deployment > issue, fixes are runbooks)",
        "entity": ("customer", "customers"),
        "context": ("deployment", "deployments"),
        "case": ("issue", "issues"),
        "playbook": ("runbook", "runbooks"),
    },
    "application": {
        "label": "Projects (customer > use-case > blocker, fixes are playbooks)",
        "entity": ("customer", "customers"),
        "context": ("use-case", "use-cases"),
        "case": ("blocker", "blockers"),
        "playbook": ("playbook", "playbooks"),
    },
    "support": {
        "label": "Support (account > environment > ticket, fixes are how-tos)",
        "entity": ("account", "accounts"),
        "context": ("environment", "environments"),
        "case": ("ticket", "tickets"),
        "playbook": ("how-to", "how-tos"),
    },
    "product": {
        "label": "Product (area > component > bug, fixes are recipes)",
        "entity": ("area", "areas"),
        "context": ("component", "components"),
        "case": ("bug", "bugs"),
        "playbook": ("recipe", "recipes"),
    },
    "generic": {
        "label": "Generic (subject > context > case, fixes are playbooks)",
        "entity": ("subject", "subjects"),
        "context": ("context", "contexts"),
        "case": ("case", "cases"),
        "playbook": ("playbook", "playbooks"),
    },
}
DEFAULT_PRESET = "deployment"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# Registry aliases are joined into a grep -E / Python re alternation (ldn_entity_pattern), so they
# are restricted to characters that are either literal in both dialects or bracket-escaped there
# (`.` and `+`). Anything else (`\`, `^`, `$`, `(`, `|`, `*`, `?`, `[`, `{`) is dropped with a
# warning: one alias `c++corp` used to make grep fail, which emptied the capture pre-filter and
# skipped every session forever.
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._&+'-]*$")


def config_path() -> Path:
    for p in CONFIG_CANDIDATES:
        if p.exists():
            return p
    return CONFIG_CANDIDATES[0]


def _parse_value(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        # list items may be quoted (`aliases: ["acme corp", acme]`): strip one matching pair
        items = [s.strip() for s in v[1:-1].split(",") if s.strip()]
        return [s[1:-1] if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'" else s for s in items]
    if v == "{}":
        return {}
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load() -> dict:
    """Committed config (leyden.yaml) with the per-person overlay (leyden.local.yaml) merged on
    top. In team mode the taxonomy lives in the committed file and each member's identity in the
    overlay, so nobody commits their own name over the team's."""
    cfg = _load_one(config_path())
    if LOCAL_CONFIG.exists():
        _deep_merge(cfg, _load_one(LOCAL_CONFIG))
    return cfg


def _load_one(path: Path) -> dict:
    if not path.exists():
        return {}
    root: dict = {}
    # stack of (indent, dict): a new deeper indent nests under the last key seen
    stack = [(-1, root)]
    for raw in path.read_text(encoding="utf-8").split("\n"):
        # strip comments only when the '#' is preceded by whitespace: a bare split on '#'
        # silently truncated values like url_base: https://host/browse/#id
        line = "" if raw.lstrip().startswith("#") else re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(":")
        key = key.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val.strip():
            parent[key] = _parse_value(val)
        else:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def get(path: str, default=""):
    node = load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _plural(word: str) -> str:
    if word.endswith("s") or word.endswith("x"):
        return word + "es"
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def taxonomy(cfg: dict | None = None) -> dict:
    """Resolved slot names. Order of precedence: explicit `taxonomy.<slot>.singular/plural` in the
    config, then the named `kb.preset`, then the deployment preset. Always returns all four slots."""
    cfg = load() if cfg is None else cfg
    preset_name = cfg.get("kb", {}).get("preset", DEFAULT_PRESET) if isinstance(cfg.get("kb"), dict) else DEFAULT_PRESET
    preset = PRESETS.get(preset_name, PRESETS[DEFAULT_PRESET])
    tax_cfg = cfg.get("taxonomy", {}) if isinstance(cfg.get("taxonomy"), dict) else {}
    out = {}
    for slot in SLOTS:
        sing, plur = preset[slot]
        node = tax_cfg.get(slot, {})
        if isinstance(node, dict):
            sing = str(node.get("singular", sing)).strip() or sing
            plur = str(node.get("plural", "")).strip() or (_plural(sing) if sing != preset[slot][0] else plur)
        elif isinstance(node, str) and node.strip():
            sing = node.strip()
            plur = _plural(sing)
        out[slot] = {"singular": sing, "plural": plur}
    return out


def dirs(cfg: dict | None = None) -> dict:
    """Directory name per slot, plus the two fixed folders."""
    T = taxonomy(cfg)
    d = {slot: T[slot]["plural"] for slot in SLOTS}
    d.update(FIXED_DIRS)
    return d


def validate_taxonomy(T: dict) -> list[str]:
    """Return a list of problems (empty = fine). Slot names become directory names, frontmatter
    `type:` values and frontmatter keys, so they must be lowercase-kebab and pairwise distinct."""
    problems = []
    seen: dict[str, str] = {}
    for slot in SLOTS:
        for form in ("singular", "plural"):
            v = T[slot][form]
            if not _SLUG_RE.match(v):
                problems.append(f"{slot}.{form} '{v}' must be lowercase-kebab (a-z, 0-9, -)")
            if v in seen and seen[v] != slot:
                problems.append(f"'{v}' is used by both {seen[v]} and {slot}")
            seen[v] = slot
        if T[slot]["singular"] == T[slot]["plural"]:
            problems.append(f"{slot}: singular and plural are both '{T[slot]['singular']}'")
    for fixed in FIXED_DIRS.values():
        if fixed in seen:
            problems.append(f"'{fixed}' is reserved (fixed folder) and cannot be a slot name")
    for reserved in ("index", "log", "project"):
        if reserved in seen:
            problems.append(f"'{reserved}' is a reserved frontmatter type and cannot be a slot name")
    return problems


def registry(cfg: dict | None = None) -> dict:
    """The per-entity enrichment map (aliases, email_domains). Lives under the entity plural key
    (`customers:` for the deployment preset) or under a literal `registry:` key. Folders on disk
    remain the registry of record; this only enriches them."""
    cfg = load() if cfg is None else cfg
    key = taxonomy(cfg)["entity"]["plural"]
    reg = cfg.get("registry")
    if not isinstance(reg, dict):
        reg = cfg.get(key)
    if not isinstance(reg, dict):
        reg = cfg.get("customers")  # legacy key from the pre-taxonomy layout
    return reg if isinstance(reg, dict) else {}


def entity_aliases(c) -> tuple:
    """(valid, rejected) alias lists for one registry entry."""
    al = c.get("aliases", []) if isinstance(c, dict) else []
    al = [al] if isinstance(al, str) else al if isinstance(al, list) else []
    good = [a for a in al if _ALIAS_RE.match(a)]
    return good, [a for a in al if not _ALIAS_RE.match(a)]


def validate_registry(reg: dict | None = None) -> list[str]:
    reg = registry() if reg is None else reg
    return [f"{slug}: alias {a!r} has characters outside [A-Za-z0-9 ._&+'-] and is ignored"
            for slug, c in reg.items() for a in entity_aliases(c)[1]]


def shell_exports(cfg: dict | None = None) -> str:
    """`export LDN_...=...` lines that every hook `eval`s. Two layers, because the values come from
    a hand-edited file and land in a shell: (1) a slot name that is not lowercase-kebab is REJECTED
    (replaced by the preset's name, warning on stderr), since it would also be a directory name and
    a frontmatter key; (2) every value is shlex-quoted anyway. Before this, `singular: x$(touch P)`
    in leyden.yaml ran `touch P` inside every hook that sourced _guard.sh."""
    cfg = load() if cfg is None else cfg
    T = taxonomy(cfg)
    preset_name = cfg.get("kb", {}).get("preset", DEFAULT_PRESET) if isinstance(cfg.get("kb"), dict) else DEFAULT_PRESET
    preset = PRESETS.get(preset_name, PRESETS[DEFAULT_PRESET])
    lines = []
    plural_var = {"entity": "ENTITIES", "context": "CONTEXTS", "case": "CASES", "playbook": "PLAYBOOKS"}
    for slot in SLOTS:
        for i, (form, var) in enumerate((("singular", f"LDN_{slot.upper()}"), ("plural", f"LDN_{plural_var[slot]}"))):
            v = T[slot][form]
            if not _SLUG_RE.match(v):
                print(f"leyden: taxonomy.{slot}.{form} {v!r} is not lowercase-kebab; using "
                      f"'{preset[slot][i]}'", file=sys.stderr)
                v = preset[slot][i]
            lines.append(f"{var}={shlex.quote(v)}")
    lines.append(f"LDN_INTERNAL={shlex.quote(FIXED_DIRS['internal'])}")
    lines.append(f"LDN_PROJECTS={shlex.quote(FIXED_DIRS['projects'])}")
    return "\n".join(f"export {l}" for l in lines)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    if cmd == "get":
        default = args[2] if len(args) > 2 else ""
        v = get(args[1], default)
        if isinstance(v, dict):
            # an empty-valued key parses as a nested dict; printing '{}' poisoned shell
            # arithmetic downstream (INTERVAL='{}' broke the push stale gate). Print the
            # caller's default instead, exactly as if the key were absent.
            v = default
        print(" ".join(v) if isinstance(v, list) else v)
        return 0
    if cmd == "shell":
        print(shell_exports())
        return 0
    if cmd == "taxonomy":
        T = taxonomy()
        for slot in SLOTS:
            print(f"{slot}\t{T[slot]['singular']}\t{T[slot]['plural']}")
        return 0
    if cmd == "validate":
        problems = [f"taxonomy: {p}" for p in validate_taxonomy(taxonomy())]
        problems += [f"registry: {p}" for p in validate_registry()]
        for p in problems:
            print(p)
        return 1 if problems else 0
    if cmd == "presets":
        for name, p in PRESETS.items():
            print(f"{name}\t{p['label']}")
        return 0
    if cmd == "preset":
        p = PRESETS.get(args[1] if len(args) > 1 else "")
        if not p:
            print(f"unknown preset: {args[1] if len(args) > 1 else ''}")
            return 2
        for slot in SLOTS:
            print(f"{slot}\t{p[slot][0]}\t{p[slot][1]}")
        return 0
    if cmd == "dir":
        print(dirs().get(args[1] if len(args) > 1 else "", ""))
        return 0
    reg = registry()
    if cmd in ("registry", "aliases"):
        for p in validate_registry(reg):
            print(f"leyden: registry: {p}", file=sys.stderr)
        for slug, c in reg.items():
            print(f"{slug}\t{'|'.join(entity_aliases(c)[0])}")
        return 0
    if cmd == "domains":
        for c in reg.values():
            if isinstance(c, dict):
                for d in c.get("email_domains", []):
                    print(d)
        return 0
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
