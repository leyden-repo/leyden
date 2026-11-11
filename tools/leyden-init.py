#!/usr/bin/env python3
"""
leyden init: create (or connect) your knowledge base and wire it to this tool checkout.

The tool and the KB are separate git repos. This checkout is the TOOL: pull-only, no data ever
goes in it. Your KB is its own private repo (default: `<you>-kb` next to the tool). init records
the KB's path in `<tool>/.kb-path` (gitignored), which every hook and verb reads.

    leyden init                       # asks nothing when git config has user.name and user.email
    leyden init --kb ~/work/me-kb --remote git@host:me/me-kb.git
    leyden init --preset application --name "Ann Example" --slug ann --email ann@example.com --yes
    leyden init --mode team-owner --preset deployment --kb-name "Deployment KB" ...
    leyden init --slot entity=customer:customers --slot context=pilot:pilots ...   # custom slots
    leyden init --write-hooks /path/to/.claude/settings.json   # also register the three hooks
    leyden init --remote git@host:me/my-kb.git                 # set YOUR private remote as origin

Defaults (no questions): KB at <tool parent>/<slug>-kb, mode individual, preset deployment,
KB name "<First name>'s KB", no ticket URL, identity from git config (slug = first name, else the
email's local part). Only a missing name or email is asked, and only on a terminal; without one,
init exits 2 naming the flag to pass. `--yes` is accepted and changes nothing then.

Guided setup for a whole workspace (KB, hooks, CLAUDE.md, skills, optional team repo, doctor):
`leyden setup --personal <url|new|local> [--team <url>]` (see `leyden setup --help`).

Three ways in:
  individual    one person, one private repo. Everything lands in leyden.yaml.
  team-owner    sets the taxonomy for a team and commits it in leyden.yaml. Their own identity goes
                to leyden.local.yaml (gitignored) like every other member's.
  team-member   clone already carries a leyden.yaml with kb.mode: team. Only identity is asked,
                written to leyden.local.yaml. The taxonomy is read-only for members.

Never destructive: existing files are left alone (reported as "kept"), the taxonomy is never
rewritten once entries exist, and .claude/settings.json is only touched with --write-hooks.

Remotes: `--remote URL` sets your KB's `origin` (a PRIVATE repo). The push refuses, fail-closed,
when the KB's origin is the Leyden tool repo. Legacy layout only (a KB living inside a clone of the
tool repo): a tool-repo `origin` is renamed to `leyden-upstream` so the push cannot publish there.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

TOOL_ROOT = _config.TOOL_ROOT
TOOLS = _config.TOOLS_DIR
TEMPLATES = TOOL_ROOT / "templates"
KB = _config.KB                 # re-pointed by use_kb() once the target KB is known
RENDERED = KB / "_templates"

# The KB's own .gitignore. Secret-shaped FILENAMES are ignored, but markdown is always knowledge
# (its secrets are redacted in place by the commit guard), and .leyden/ holds machine-local state.
KB_GITIGNORE = """\
# Leyden runtime state: logs, capture queue, locks, offsets, search index (machine-local)
.leyden/

# Never commit live secrets: reference where they live instead (see CONVENTIONS.md)
.env
.env.*
*.key
*.pem
*.pfx
*.p12
*_secret*
*secret.*
*credentials*
*.kubeconfig
kubeconfig*
id_rsa*
*.token
# ...but markdown is KNOWLEDGE even when its name looks secret-shaped
# (runbooks/rotate-client-secret.md): its secrets are redacted by the commit guard instead.
!*.md

# generated views (rebuild with `leyden dash` / `leyden graph`)
graph.html
issues.html
.promote-staging/

# per-person overlay in team mode (identity only)
leyden.local.yaml

# OS / editor noise
.DS_Store
Thumbs.db
*.swp
.idea/
.vscode/
__pycache__/
"""


def use_kb(path: Path, source: str) -> None:
    global KB, RENDERED
    _config.set_kb(path, source)
    KB = _config.KB
    RENDERED = KB / "_templates"


def inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def git_identity() -> tuple:
    """(user.name, user.email) from git config, "" when unset."""
    def get(key: str) -> str:
        return subprocess.run(["git", "config", key], capture_output=True, text=True).stdout.strip()
    return get("user.name"), get("user.email")


def _kebab(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    t = re.sub(r"^[^a-z]+", "", t)   # a slug starts with a letter
    return t


def derive_slug(name: str, email: str) -> str:
    """First name lowercased-kebab, else the local part of the email, else "me"."""
    for cand in ((name or "").split()[:1] + [(email or "").split("@")[0]]):
        k = _kebab(cand)
        if k and SLUG_RE.match(k):
            return k
    return "me"


def default_kb_name(name: str, slug: str) -> str:
    first = ((name or "").split() or [slug])[0].rstrip(".,") or slug
    return f"{first}'s KB"


def default_kb_path(a=None) -> Path:
    """<tool parent>/<slug>-kb, the slug from --slug/--name/--email when given, else git config."""
    name, email = git_identity()
    name = (getattr(a, "name", None) or name or os.environ.get("USER", ""))
    email = getattr(a, "email", None) or email
    slug = getattr(a, "slug", None) or derive_slug(name, email)
    return TOOL_ROOT.parent / f"{slug}-kb"

# Files only the Leyden tool repo tracks. A KB whose history ever touched them was cloned from the
# tool repo. Renaming or moving either file in the tool silently disables that check: update here.
TOOL_MARKER_FILES = ("bin/leyden", "tools/leyden-init.py")
UPSTREAM_REMOTE = "leyden-upstream"
HOOK_SCRIPTS = (("SessionStart", "leyden-load.sh"), ("UserPromptSubmit", "leyden-search.sh"),
                ("SessionEnd", "leyden-enqueue.sh"))


# ── prompting ─────────────────────────────────────────────────────────────────────────────────────

def ask(label: str, default: str = "", yes: bool = False, validate=None) -> str:
    while True:
        if yes or not sys.stdin.isatty():
            v = default
        else:
            suffix = f" [{default}]" if default else ""
            v = input(f"{label}{suffix}: ").strip() or default
        if validate is None or validate(v):
            return v
        if yes or not sys.stdin.isatty():
            sys.exit(f"init: invalid value for {label}: '{v}'")
        print("  not valid, try again")


def need(label: str, flag: str, git_key: str, yes: bool, fallback: str) -> str:
    """A value init cannot derive from git config: asked on a terminal, the fallback with --yes,
    otherwise a one-line exit naming the flag (no half-made KB: nothing is written yet)."""
    if yes:
        return fallback
    if sys.stdin.isatty():
        return ask(label, fallback, validate=lambda v: bool(v.strip()))
    print(f"leyden init: git {git_key} is not set; pass {flag} <value> "
          f"(or run: git config --global {git_key} <value>)", file=sys.stderr)
    sys.exit(2)


# ── taxonomy ──────────────────────────────────────────────────────────────────────────────────────

def parse_slot_flags(flags: list[str]) -> dict:
    out = {}
    for f in flags or []:
        m = re.match(r"^(entity|context|case|playbook)=([a-z][a-z0-9-]*)(?::([a-z][a-z0-9-]*))?$", f)
        if not m:
            sys.exit(f"init: bad --slot '{f}' (want slot=singular[:plural])")
        slot, sing, plur = m.groups()
        out[slot] = {"singular": sing, "plural": plur or _config._plural(sing)}
    return out


def build_taxonomy(preset: str, overrides: dict, yes: bool) -> dict:
    base = _config.PRESETS.get(preset, _config.PRESETS[_config.DEFAULT_PRESET])
    T = {s: {"singular": base[s][0], "plural": base[s][1]} for s in _config.SLOTS}
    if preset == "custom" and not overrides:
        print("\nName the four slots (lowercase-kebab). Enter keeps the default.")
        for slot in _config.SLOTS:
            hint = {"entity": "who the work is for", "context": "a concrete setup under an entity",
                    "case": "one thing that went wrong or was asked",
                    "playbook": "a reusable fix or technique"}[slot]
            sing = ask(f"  {slot} ({hint}), singular", T[slot]["singular"], yes, SLUG_RE.match)
            plur = ask(f"  {slot} plural", _config._plural(sing), yes, SLUG_RE.match)
            T[slot] = {"singular": sing, "plural": plur}
    for slot, v in overrides.items():
        T[slot] = v
    problems = _config.validate_taxonomy(T)
    if problems:
        sys.exit("init: taxonomy invalid:\n  " + "\n  ".join(problems))
    return T


def preset_matches(T: dict, preset: str) -> bool:
    p = _config.PRESETS.get(preset)
    return bool(p) and all(T[s] == {"singular": p[s][0], "plural": p[s][1]} for s in _config.SLOTS)


# ── rendering ─────────────────────────────────────────────────────────────────────────────────────

def variables(T: dict, cfg: dict) -> dict:
    now = datetime.now(timezone.utc)
    v = {
        "kb_name": cfg["kb"]["name"],
        "preset": cfg["kb"]["preset"],
        "mode": cfg["kb"]["mode"],
        "author": cfg["engineer"]["slug"],
        "ticket_url_base": cfg["ticketing"]["url_base"],
        "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today": now.strftime("%Y-%m-%d"),
    }
    plural_key = {"entity": "entities", "context": "contexts", "case": "cases", "playbook": "playbooks"}
    for slot in _config.SLOTS:
        s, p = T[slot]["singular"], T[slot]["plural"]
        pk = plural_key[slot]
        v[slot] = s
        v[pk] = p
        v[slot.capitalize()] = s.capitalize()
        v[pk.capitalize()] = p.capitalize()
    # templates use {{entity}} {{entities}} {{Entity}} {{Entities}} and the same for the other
    # three slots. Nothing else is substituted.
    return v


def render(text: str, v: dict) -> str:
    def sub(m):
        key = m.group(1)
        return str(v.get(key, m.group(0)))
    return re.sub(r"\{\{(\w+)\}\}", sub, text)


MD_START, MD_END = "<!-- leyden:start -->", "<!-- leyden:end -->"


def write_claude_md(workspace: Path | None) -> str:
    """Write or refresh the Leyden block in <workspace>/CLAUDE.md between the leyden markers.
    Everything outside the markers is the user's and is never touched. workspace None = no project
    to write into (user-level hooks): print the block instead."""
    T = _config.taxonomy(); D = _config.dirs()
    try:
        kb_rel = KB.resolve().relative_to(workspace.resolve()).as_posix() if workspace else str(KB)
    except ValueError:
        kb_rel = str(KB)
    try:
        tool_rel = TOOL_ROOT.resolve().relative_to(workspace.resolve()).as_posix() if workspace else str(TOOL_ROOT)
    except ValueError:
        tool_rel = str(TOOL_ROOT)
    v = {"kb": kb_rel, "tool": "./" + tool_rel if not tool_rel.startswith("/") else tool_rel, "entity": T["entity"]["singular"], "entities": T["entity"]["plural"],
         "case": T["case"]["singular"], "cases": T["case"]["plural"], "playbooks": D["playbook"]}
    team_block = ""
    team = str(_config.get("team.shared_dir", "") or "").strip()
    if team:
        tp = Path(team).expanduser()
        if not tp.is_absolute():
            tp = Path(os.path.normpath(str(KB / tp)))   # team join stores it relative to the KB
        try:
            v["team_dir"] = tp.resolve().relative_to(workspace.resolve()).as_posix() if workspace else str(tp)
        except (ValueError, AttributeError):
            v["team_dir"] = str(tp)
        team_block = render((TEMPLATES / "claude-md-team.md").read_text(encoding="utf-8"), v)
    v["team_block"] = team_block
    block = render((TEMPLATES / "claude-md-block.md").read_text(encoding="utf-8"), v).rstrip() + "\n"
    block = f"{MD_START}\n{block}{MD_END}\n"
    if workspace is None:
        return ("CLAUDE.md: hooks are not in a project's .claude/, so no workspace CLAUDE.md was written. "
                "Add this to the CLAUDE.md of the folder you start Claude in:\n" + block)
    path = workspace / "CLAUDE.md"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    si = old.find(MD_START)
    ei = old.find(MD_END, si + len(MD_START)) if si >= 0 else -1
    if si >= 0 and ei < 0:
        # a start marker with no end after it: guessing where the block ends could delete user text
        return (f"CLAUDE.md: {path} has `{MD_START}` with no `{MD_END}` after it; fix the markers by "
                "hand, then re-run. Nothing was written.")
    if si >= 0:
        tail = old[ei + len(MD_END):].lstrip("\n")
        new = old[:si] + block + (("\n" + tail) if tail else "")
    else:
        new = (old.rstrip() + "\n\n" if old.strip() else "") + block
    if new == old:
        return f"CLAUDE.md: Leyden block already current in {path}"
    path.write_text(new, encoding="utf-8")
    return f"CLAUDE.md: Leyden block {'updated' if MD_START in old else 'added'} in {path}"


def write_if_absent(path: Path, content: str, report: list) -> None:
    if path.exists():
        report.append(("kept", path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    report.append(("wrote", path))


# ── config emit ───────────────────────────────────────────────────────────────────────────────────

def emit_kb_block(cfg: dict, T: dict, include_taxonomy: bool) -> str:
    lines = [
        "# Leyden configuration. Written by `leyden init`; format documented in leyden.yaml.example.",
        "# The taxonomy below names directories and frontmatter keys: do not rename slots once",
        "# entries exist (it means renaming every folder and key across the KB).",
        "",
        "kb:",
        f"  name: {cfg['kb']['name']}",
        f"  mode: {cfg['kb']['mode']}",
        f"  preset: {cfg['kb']['preset']}",
    ]
    if include_taxonomy:
        lines += ["", "taxonomy:"]
        for slot in _config.SLOTS:
            lines += [f"  {slot}:", f"    singular: {T[slot]['singular']}", f"    plural: {T[slot]['plural']}"]
    lines += [
        "",
        "ticketing:",
        f"  provider: {cfg['ticketing']['provider']}    # informational only: no tool reads it",
        f"  url_base: {cfg['ticketing']['url_base']}",
        "",
        "push:",
        "  interval_seconds: 72000",
        "",
        "capture:",
        "  model: sonnet",
        "",
        "retrieval:",
        "  max_rank: 500",
        "  min_evidence: 5",
        "",
        f"# per-{T['entity']['singular']} enrichment (aliases; email_domains is informational, capture does not read it yet);",
        "# folders on disk are the registry of record",
        f"{T['entity']['plural']}:",
        "",
    ]
    return "\n".join(lines)


def emit_identity_block(cfg: dict) -> str:
    return "\n".join([
        "# Leyden per-person overlay (gitignored). Merged on top of leyden.yaml by tools/_config.py.",
        "engineer:",
        f"  name: {cfg['engineer']['name']}    # informational only",
        f"  slug: {cfg['engineer']['slug']}",
        f"  email: {cfg['engineer']['email']}",
        "",
    ])


# ── remotes ───────────────────────────────────────────────────────────────────────────────────────
# `git clone <leyden> my-kb` leaves origin = the TOOL repo, and the batched push (`git add -A`,
# commit, push) would then publish the whole KB (every entry, leyden.yaml with your email)
# to the tool repo. The signal for "origin is the tool repo": some commit reachable from HEAD
# touched the tool's own files (deleting them later does not hide it) AND the origin URL is named leyden(.git), unless `init --remote` recorded that URL as the
# KB's own (git config leyden.kbOrigin), for a private KB that happens to be called leyden too.

def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(KB), *args], capture_output=True, text=True)


def remote_url(name: str) -> str:
    r = git("remote", "get-url", name)
    return r.stdout.strip() if r.returncode == 0 else ""


def _norm_url(u: str) -> str:
    u = u.strip().rstrip("/")
    return u[:-4] if u.endswith(".git") else u


def has_tool_history() -> bool:
    return all(git("log", "-1", "--format=%H", "HEAD", "--", f).stdout.strip()
               for f in TOOL_MARKER_FILES)


def looks_like_tool_url(url: str) -> bool:
    base = re.split(r"[/:]", _norm_url(url))[-1]
    return base == "leyden"


def origin_is_tool(origin: str) -> bool:
    if not origin:
        return False
    up = remote_url(UPSTREAM_REMOTE)
    if up and _norm_url(up) == _norm_url(origin):
        return True
    ack = git("config", "--get", "leyden.kbOrigin").stdout.strip()
    if ack and _norm_url(ack) == _norm_url(origin):
        return False
    return looks_like_tool_url(origin) and has_tool_history()


def tool_urls() -> set:
    """Every spelling of the tool repo a KB origin must never equal: the tool checkout's own
    origin URL and the checkout path itself (a local clone used as a remote)."""
    out = {_norm_url(str(TOOL_ROOT)), _norm_url("file://" + str(TOOL_ROOT))}
    r = subprocess.run(["git", "-C", str(TOOL_ROOT), "remote", "get-url", "origin"],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        out.add(_norm_url(r.stdout.strip()))
    return out


def is_tool_url(url: str) -> bool:
    if not url:
        return False
    u = _norm_url(url)
    if u in tool_urls():
        return True
    p = Path(url.replace("file://", "", 1)).expanduser()
    return p.exists() and p.resolve() == TOOL_ROOT


def legacy() -> bool:
    return KB.resolve() == TOOL_ROOT


def push_urls(name: str) -> list:
    """Every URL a push to remote <name> would actually reach: `get-url --push --all` applies
    remote.<name>.pushurl AND url.<base>.pushInsteadOf/insteadOf rewrites, which is what git push
    itself uses. Checking only the fetch URL let all three of those route the KB to the tool repo
    while the guard said OK (reproduced)."""
    r = git("remote", "get-url", "--push", "--all", name)
    return [u.strip() for u in r.stdout.splitlines() if u.strip()] if r.returncode == 0 else []


def push_target_problem() -> str:
    """A push (no arguments) goes to branch.<b>.pushRemote, else remote.pushDefault, else the
    branch's upstream remote, else origin. Any of those that is not origin, or any URL a push to
    origin would reach that is the tool repo, is refused."""
    branch = git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    targets = []
    if branch:
        pr = git("config", "--get", f"branch.{branch}.pushRemote").stdout.strip()
        if pr:
            targets.append((f"branch.{branch}.pushRemote", pr))
    pd = git("config", "--get", "remote.pushDefault").stdout.strip()
    if pd:
        targets.append(("remote.pushDefault", pd))
    for key, name in targets:
        if name != "origin":
            return (f"{key} is '{name}', so a push would go there instead of origin. "
                    f"Run: git -C {shlex.quote(str(KB))} config --unset {key}")
    for u in push_urls("origin"):
        if is_tool_url(u) or (looks_like_tool_url(u) and has_tool_history()):
            return (f"a push to origin would reach {u}, the Leyden tool repo (remote.origin.pushurl or "
                    f"a url.*.pushInsteadOf rule). Run: git -C {shlex.quote(str(KB))} remote set-url --push origin <your-private-repo-url>")
    return ""


def origin_problem() -> str:
    """Why pushing this KB would publish it somewhere it must not go; "" when it is safe."""
    if git("rev-parse", "--git-dir").returncode != 0:
        return ""
    if not legacy():
        p = push_target_problem()
        if p:
            return p
    origin = remote_url("origin")
    if origin and not legacy() and is_tool_url(origin):
        return (f"origin ({origin}) is the Leyden tool repo; your KB would be published there. "
                f"Run: leyden init --remote <your-private-repo-url>")
    if origin and origin_is_tool(origin):
        return (f"origin ({origin}) is the Leyden tool repo; your KB would be published there. "
                f"Run: leyden init --remote <your-private-repo-url>")
    branch = git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if branch:
        tracked = git("config", "--get", f"branch.{branch}.remote").stdout.strip()
        if tracked and tracked not in ("origin", "."):
            return (f"branch {branch} tracks '{tracked}' ({remote_url(tracked) or 'no url'}), not origin, "
                    f"so a push would go there. Run: git -C {shlex.quote(str(KB))} branch --unset-upstream")
    return ""


def fix_remotes(remote: str | None) -> list:
    """Move a tool-repo origin out of the way and set the user's own one. Returns report lines."""
    out = []
    if git("rev-parse", "--git-dir").returncode != 0:
        if remote:
            out.append(f"not a git repo, --remote ignored: run `git init` in {KB} first")
        return out
    if remote and not legacy() and is_tool_url(remote):
        sys.exit(f"init: --remote {remote} is the Leyden tool repo. Your KB needs its OWN private repo.")
    origin = remote_url("origin")
    if origin and legacy() and (remote is None or _norm_url(remote) != _norm_url(origin)) and origin_is_tool(origin):
        if remote_url(UPSTREAM_REMOTE):
            git("remote", "remove", "origin")
            out.append(f"removed origin ({origin}): same as {UPSTREAM_REMOTE}, the Leyden tool repo")
        else:
            git("remote", "rename", "origin", UPSTREAM_REMOTE)
            out.append(f"renamed origin -> {UPSTREAM_REMOTE} ({origin}): it is the Leyden tool repo, "
                       "and the batched push would have published your KB there")
        origin = ""
    if remote:
        if origin:
            git("remote", "set-url", "origin", remote)
        else:
            git("remote", "add", "origin", remote)
        git("config", "leyden.kbOrigin", remote)
        out.append(f"origin = {remote} (your KB remote; the batched push sends commits here)")
        origin = remote
    # A branch still tracking the tool repo would make `git push` (no arguments) go there.
    branch = git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if branch and git("config", "--get", f"branch.{branch}.remote").stdout.strip() == UPSTREAM_REMOTE:
        git("branch", "--unset-upstream")
        out.append(f"branch {branch} no longer tracks {UPSTREAM_REMOTE} (the first push sets origin)")
    if not origin:
        out.append("no origin yet: add your own PRIVATE repo with `leyden init --remote <url>` "
                   "(or `git remote add origin <url>`); until then nothing is pushed")
    return out


# ── hooks ─────────────────────────────────────────────────────────────────────────────────────────

def project_dir_for(settings: Path | None) -> Path | None:
    """The project a <project>/.claude/settings(.local).json belongs to; None for the user-level
    ~/.claude/settings.json (no fixed project dir there) or any other location."""
    if settings is None:
        return None
    s = Path(os.path.abspath(str(settings.expanduser())))
    if s.parent.name != ".claude" or s.name not in ("settings.json", "settings.local.json"):
        return None
    proj = s.parent.parent
    if proj.resolve() == Path.home().resolve():
        return None
    return proj


def _kb_env_prefix(settings: Path | None) -> str:
    """`LEYDEN_HOME="$CLAUDE_PROJECT_DIR"/<kb> ` when the KB lives inside the project the settings
    file belongs to, so a workspace's hooks serve THAT workspace's KB even when a tool checkout is
    shared. Never an absolute path: a baked-in absolute KB silently died when the workspace moved,
    and a COPIED workspace kept capturing into the original's KB (reproduced). Elsewhere none: the
    tool's .kb-path (relative to the workspace) decides. Legacy (KB = tool checkout) needs none."""
    try:
        kb = KB
    except NameError:
        return ""
    proj = project_dir_for(settings)
    if proj is None or kb is None or _config.KB_SOURCE == "none" or kb.resolve() == TOOL_ROOT:
        return ""
    try:
        rel = kb.resolve().relative_to(proj.resolve())
    except ValueError:
        return ""
    return f'LEYDEN_HOME="$CLAUDE_PROJECT_DIR"/{shlex.quote(rel.as_posix())} LDN_KB_SOURCE=hook '


def hook_command(script: str, settings: Path | None) -> str:
    """Shell-safe command string for one hook. Claude Code runs it through a shell, so a path
    with a space in it must be quoted: unquoted, every hook failed (exit 126) without a word,
    while doctor still said PASS. Under the project's own .claude/settings.json the path is
    written relative to "$CLAUDE_PROJECT_DIR", so the registration survives the workspace
    moving; anywhere else it is the absolute, quoted path."""
    target = TOOLS / script
    env = _kb_env_prefix(settings)
    proj = project_dir_for(settings)
    if proj is not None:
        try:
            rel = target.relative_to(proj.resolve())
            return env + '"$CLAUDE_PROJECT_DIR"/' + shlex.quote(rel.as_posix())
        except ValueError:
            pass
    return env + shlex.quote(str(target))


def hook_json(settings: Path | None = None) -> dict:
    return {event: [{"hooks": [{"type": "command", "command": hook_command(script, settings)}]}]
            for event, script in HOOK_SCRIPTS}


SKILLS_DIR = TOOLS.parent / "skills"


def link_skills(settings: Path) -> list:
    """Symlink the skills shipped with the tool (skills/<name>/SKILL.md, e.g. kb-sweep) into the
    project's .claude/skills/, so `/kb-sweep` works there and follows tool updates. Only for a
    project-level settings file; an existing skill of the same name is never replaced."""
    proj = project_dir_for(settings)
    if proj is None or not SKILLS_DIR.is_dir():
        return []
    out = []
    for src in sorted(SKILLS_DIR.iterdir()):
        if not (src / "SKILL.md").is_file():
            continue
        dst = proj / ".claude" / "skills" / src.name
        if dst.exists() or dst.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Relative link: an absolute one followed the ORIGINAL tool after the workspace moved or was copied.
        dst.symlink_to(os.path.relpath(str(src.resolve()), str(dst.parent.resolve())), target_is_directory=True)
        out.append(src.name)
    return out


def settings_record(settings: Path) -> str:
    s = Path(os.path.abspath(str(settings.expanduser())))
    if inside(s, KB.parent):
        return os.path.relpath(str(s), str(KB))
    return str(s)


def recorded_settings() -> Path | None:
    """The settings file `--write-hooks` recorded (relative values are relative to the KB)."""
    rec = git("config", "--get", "leyden.hooksSettings").stdout.strip()
    if not rec:
        return None
    p = Path(rec).expanduser()
    return p if p.is_absolute() else Path(os.path.normpath(str(KB / p)))


def hooks_installed() -> Path | None:
    """The recorded settings file when it already carries exactly the hooks init would write."""
    s = recorded_settings()
    try:
        data = json.loads(s.read_text(encoding="utf-8") or "{}") if s and s.is_file() else None
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return None
    for event, script in HOOK_SCRIPTS:
        cmds = [h.get("command") for e in data["hooks"].get(event) or [] if isinstance(e, dict)
                for h in e.get("hooks") or [] if isinstance(h, dict)]
        if hook_command(script, s) not in cmds:
            return None
    return s


def _is_local_url(url: str) -> bool:
    if url.startswith("file://"):
        return True
    if "://" in url:
        return False
    # scp form host:path has a colon before any slash; a local path does not
    head = url.split("/", 1)[0]
    return ":" not in head


def ls_remote(url: str) -> tuple:
    """(code, refs-or-message): 0 reachable (refs text, may be empty), 1 the repo does not exist,
    3 could not tell (network, auth, timeout). Never prompts: no terminal is assumed."""
    if _is_local_url(url):
        p = Path(url[len("file://"):] if url.startswith("file://") else url).expanduser()
        if not p.is_absolute():
            p = Path(os.path.normpath(str(KB / p)))   # KB may not exist yet, so resolve ".." lexically
        if not p.exists():
            return 1, f"{url} does not exist"
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if not (env.get("GIT_SSH_COMMAND") or env.get("GIT_SSH")):
        ssh = subprocess.run(["git", "config", "--get", "core.sshCommand"], capture_output=True, text=True).stdout.strip()
        if not ssh:
            env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=10"   # never hang on a prompt
    cwd = str(KB) if (KB / ".git").exists() else None   # the KB's url.*.insteadOf rules apply
    try:
        r = subprocess.run(["git", "ls-remote", "--heads", url], capture_output=True, text=True,
                           env=env, cwd=cwd, stdin=subprocess.DEVNULL, timeout=30)
    except subprocess.TimeoutExpired:
        return 3, f"{url} did not answer within 30 s"
    if r.returncode == 0:
        return 0, r.stdout
    err = (r.stderr.strip().splitlines() or ["git ls-remote failed"])[-1]
    low = r.stderr.lower()
    if _is_local_url(url) or any(k in low for k in ("not found", "does not exist", "does not appear to be a git repository")):
        return 1, f"{url}: {err}"
    return 3, f"{url}: {err}"


def remote_history_problem(url: str, refs: str) -> str:
    """Why <url> cannot be this KB's origin because it already holds ANOTHER history: two KBs given
    the same remote both used to pass init and then fought over it on every push."""
    heads = [l.split("\t", 1)[0] for l in refs.splitlines() if "\t" in l]
    if not heads:
        return ""
    clone_hint = (f"Use a new, EMPTY private repo for this KB, or, if {url} is your existing KB, clone it "
                  f"and point init at the clone: git clone {url} <path> && leyden init --kb <path>")
    local_roots = set(git("rev-list", "--max-parents=0", "HEAD").stdout.split())
    if not local_roots:
        return f"{url} already has commits and this KB has none (it is another KB's repo). {clone_hint}"
    if all(git("cat-file", "-e", h + "^{commit}").returncode == 0 for h in heads):
        return ""
    ns = "refs/leyden-remote-check/"
    git("fetch", "-q", "--no-tags", url, f"+refs/heads/*:{ns}*")
    refs_here = git("for-each-ref", "--format=%(refname)", ns).stdout.split()
    remote_roots = set(git("rev-list", "--max-parents=0", *refs_here).stdout.split()) if refs_here else set()
    for ref in refs_here:
        git("update-ref", "-d", ref)
    if remote_roots and not (remote_roots & local_roots):
        return f"{url} holds a history unrelated to this KB (no common root commit). {clone_hint}"
    return ""


def settings_problem(settings: Path) -> str:
    """Why settings.json cannot be updated safely; "" when it can (or does not exist yet)."""
    if not settings.exists():
        return ""
    try:
        data = json.loads(settings.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        where = f" (line {e.lineno}, column {e.colno})" if isinstance(e, json.JSONDecodeError) else ""
        return f"{settings} is not valid JSON{where}. Fix it by hand and re-run; it was not changed."
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        return (f"{settings} must be a JSON object whose \"hooks\" is an object. Fix it by hand and "
                "re-run; it was not changed.")
    return ""


def _runs_script(command: str, script: str) -> bool:
    """True when a hook command runs this exact script file (not one whose name merely contains it,
    like a user's backup-before-leyden-load.sh.wrapper)."""
    return re.search(r"(?:^|[/\s\"'])" + re.escape(script) + r"(?:$|[\s\"';&|])", command) is not None


def write_hooks(settings: Path) -> str:
    why = settings_problem(settings)
    if why:
        raise SystemExit(f"leyden: {why}")
    data = {}
    if settings.exists():
        data = json.loads(settings.read_text(encoding="utf-8") or "{}")
    hooks = data.setdefault("hooks", {})
    added, updated = [], []
    for event, script in HOOK_SCRIPTS:
        cmd = hook_command(script, settings)
        existing = hooks.setdefault(event, [])
        # Dedupe on the SCRIPT NAME, not the exact command: a moved or re-cloned KB used to add a
        # second full set next to the stale one, so every prompt ran the search twice (once
        # against a path that no longer existed). The first registration is updated in place,
        # any further ones are dropped.
        seen = False
        for entry in existing:
            kept = []
            for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
                c = h.get("command", "") if isinstance(h, dict) else ""
                if not _runs_script(c, script):
                    kept.append(h)
                elif not seen:
                    seen = True
                    if c != cmd:
                        h["command"] = cmd
                        updated.append(event)
                    kept.append(h)
                else:
                    updated.append(event)
            if isinstance(entry, dict) and "hooks" in entry:
                entry["hooks"] = kept
        existing[:] = [e for e in existing if not (isinstance(e, dict) and "hooks" in e and not e["hooks"])]
        if not seen:
            existing.append({"hooks": [{"type": "command", "command": cmd}]})
            added.append(event)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Remember where the hooks went, so `leyden doctor` checks the file init actually wrote
    # (git config is per clone and never committed). Relative to the KB when it sits in the same
    # workspace: an absolute path made a copied workspace's doctor check (and "fix") the original's.
    git("config", "leyden.hooksSettings", settings_record(settings))
    parts = []
    if added:
        parts.append("registered " + ", ".join(added))
    if updated:
        parts.append("updated " + ", ".join(sorted(set(updated))))
    linked = link_skills(settings)
    if linked:
        parts.append("linked skill " + ", ".join(linked))
    return f"{'; '.join(parts) or 'hooks already registered'} in {settings}"


# ── main ──────────────────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(prog="leyden init", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["individual", "team-owner", "team-member"])
    ap.add_argument("--preset", choices=[*_config.PRESETS, "custom"])
    ap.add_argument("--slot", action="append", help="slot=singular[:plural], repeatable (implies custom)")
    ap.add_argument("--kb-name")
    ap.add_argument("--name"); ap.add_argument("--slug"); ap.add_argument("--email")
    ap.add_argument("--ticket-url", help="ticket URL base, e.g. https://linear.app/org/issue/")
    ap.add_argument("--write-hooks", metavar="SETTINGS_JSON", help="register the 3 hooks in this settings file")
    ap.add_argument("--kb", metavar="PATH", help="where your KB lives (its own git repo, outside the tool); "
                    "default: <tool parent>/<you>-kb")
    ap.add_argument("--remote", metavar="URL", help="your own private repo for this KB; becomes `origin`")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="never ask; a value git config cannot supply falls back to a default")
    ap.add_argument("--force", action="store_true",
                    help="re-point this tool checkout at a different KB (see .kb-path)")
    ap.add_argument("--no-doctor", action="store_true")
    ap.add_argument("--check-origin", action="store_true",
                    help="exit 1 (and say why) if pushing would publish this KB to the tool repo")
    ap.add_argument("--print-hooks", metavar="SETTINGS_JSON", nargs="?", const="",
                    help="print the hooks block for this settings file, write nothing")
    ap.add_argument("--check-remote", action="store_true",
                    help="exit 0 if the KB's origin answers, 1 if it does not exist, 3 if unreachable")
    ap.add_argument("--claude-md-only", action="store_true",
                    help="only write/refresh the Leyden block in <workspace>/CLAUDE.md")
    ap.add_argument("--workspace", metavar="DIR", help="workspace for --claude-md-only (default: tool's parent)")
    a = ap.parse_args()
    yes = a.yes
    if a.claude_md_only:
        if _config.KB_SOURCE == "none":
            print("no KB configured, run: leyden init")
            return 1
        print(write_claude_md(Path(a.workspace).expanduser() if a.workspace
                              else project_dir_for(recorded_settings())))
        return 0
    if a.slug is not None and not SLUG_RE.match(a.slug):
        # the flag skipped the validation the prompt applies; a bad slug lands in every author: field
        sys.exit(f"init: --slug '{a.slug}' must be lowercase-kebab (a-z, 0-9, -), starting with a letter")

    if a.kb:
        use_kb(Path(a.kb).expanduser().resolve(), "kb-path")
    if a.check_origin:
        if _config.KB_SOURCE == "none":
            print("no KB configured, run: leyden init")
            return 1
        problem = origin_problem()
        if problem:
            print(problem)
            return 1
        return 0
    if a.check_remote:
        if _config.KB_SOURCE == "none":
            print("no KB configured, run: leyden init")
            return 1
        origin = remote_url("origin")
        if not origin:
            return 0
        code, out = ls_remote(origin)
        print(f"origin {origin} answers" if code == 0 else out)
        return code
    if a.print_hooks is not None:
        print(json.dumps(hook_json(Path(a.print_hooks) if a.print_hooks else None), indent=2))
        return 0
    # ── where the KB lives (the tool checkout never holds data)
    if a.kb:
        target, source = Path(a.kb).expanduser().resolve(), "kb-path"
    elif _config.KB_SOURCE != "none":
        target, source = _config.KB, _config.KB_SOURCE
    else:
        # The KB folder is named after the slug, so a missing identity is settled first.
        gn, ge = git_identity()
        a.name = a.name or gn or need("Your name", "--name", "user.name", yes, os.environ.get("USER", ""))
        a.email = a.email or ge or need("Work email", "--email", "user.email", yes, "")
        target, source = default_kb_path(a).resolve(), "kb-path"   # derived, never asked
    if source == "legacy" and target == TOOL_ROOT:
        print("init: legacy layout, the KB lives inside the tool checkout. It keeps working; to move "
              "it to its own repo run: leyden init --kb <path outside the tool>")
    elif inside(target, TOOL_ROOT):
        sys.exit(f"init: {target} is inside the tool checkout ({TOOL_ROOT}). The KB must be its own repo "
                 f"outside it, e.g. --kb {default_kb_path()}")
    elif inside(TOOL_ROOT, target):
        # The KB repo would contain the tool checkout: the commit guard then adds it as a gitlink and
        # recall returns the tool's own templates and skills as KB hits (reproduced).
        sys.exit(f"init: {target} contains the tool checkout ({TOOL_ROOT}). Put the KB next to it, not "
                 f"around it, e.g. --kb {default_kb_path()}")
    if source == "kb-path" and _config.KB_PATH_FILE.is_file() and not a.force:
        old = _config.read_kb_path_file()
        if old and old.is_dir() and old.resolve() != target:
            sys.exit(f"init: this tool checkout already serves the KB at {old}.\n"
                     f"  Re-pointing it at {target} would make every workspace that uses this checkout "
                     f"capture into the new KB.\n"
                     f"  Use a second clone of the tool for a second KB, or re-run with --force.")
    # Nothing is created until every answer is in and validated (git init used to run first, so a
    # bad --remote or Ctrl-D at a prompt left a half-initialised KB behind).
    use_kb(target, source)
    if a.write_hooks and inside(Path(os.path.abspath(os.path.expanduser(a.write_hooks))), TOOL_ROOT):
        sys.exit(f"init: --write-hooks {a.write_hooks} is inside the tool checkout ({TOOL_ROOT}), which "
                 f"holds no data. Pass your WORKSPACE's settings file, e.g. --write-hooks "
                 f"{TOOL_ROOT.parent}/.claude/settings.json")

    existing = _config.config_path()
    existing_cfg = _config._load_one(existing) if existing.exists() else {}
    existing_mode = (existing_cfg.get("kb") or {}).get("mode", "") if isinstance(existing_cfg.get("kb"), dict) else ""
    if a.mode == "team-member" and not existing_cfg:
        sys.exit(f"init: --mode team-member needs a KB that already carries the team's leyden.yaml "
                 f"(kb.mode: team), and {target} has none. Clone the team KB first and point --kb at "
                 f"the clone, or use --mode team-owner to set one up.")

    # ── mode
    if a.mode:
        mode = a.mode
    elif existing_cfg and existing_mode == "team":
        mode = "team-member"
    elif existing_cfg:
        print(f"init: {existing.name} already exists (mode {existing_mode or 'individual'}). "
              "Only MISSING directories, templates and seeds are created; every existing file, "
              "the identity and the taxonomy are kept as they are.")
        mode = "reinit"
    else:
        mode = "individual"   # team-owner only with --mode; a personal KB joins a team with `leyden team join`

    # ── taxonomy + kb block
    if mode in ("team-member", "reinit"):
        cfg = existing_cfg
        T = _config.taxonomy(cfg)
        preset = (cfg.get("kb") or {}).get("preset", _config.DEFAULT_PRESET)
        kb_name = (cfg.get("kb") or {}).get("name", "Leyden")
        ticket_url = ((cfg.get("ticketing") or {}).get("url_base", "") if isinstance(cfg.get("ticketing"), dict) else "")
    else:
        overrides = parse_slot_flags(a.slot)
        preset = a.preset or ("custom" if overrides else _config.DEFAULT_PRESET)
        T = build_taxonomy(preset, overrides, yes)
        if preset == "custom":
            for name in _config.PRESETS:
                if preset_matches(T, name):
                    preset = name
        kb_name = a.kb_name   # filled in after identity: "<First name>'s KB" by default
        ticket_url = a.ticket_url or ""

    # ── identity
    if mode == "reinit":
        eng = existing_cfg.get("engineer") or {}
        local = _config._load_one(_config.LOCAL_CONFIG).get("engineer") or {}
        eng = {**eng, **local} if isinstance(local, dict) else eng
        name, slug, email = eng.get("name", ""), eng.get("slug", "me"), eng.get("email", "")
    else:
        git_name, git_email = git_identity()
        name = a.name or git_name or need("Your name", "--name", "user.name", yes, os.environ.get("USER", ""))
        email = a.email or git_email or need("Work email", "--email", "user.email", yes, "")
        slug = a.slug or derive_slug(name, email)
    if not kb_name:
        kb_name = (default_kb_name(name, slug) if mode == "individual" else
                   {"deployment": "Deployment KB", "application": "Application KB", "support": "Support KB",
                    "product": "Product KB"}.get(preset, "Knowledge Base"))

    if mode in ("reinit", "team-member") and existing_cfg:
        # A re-run keeps the existing config, so flags that disagree with it are NOT applied. Say
        # so instead of ignoring them silently (the taxonomy names directories and keys; identity
        # lives in the config file, edit it there).
        have = {"--preset": preset, "--kb-name": kb_name, "--ticket-url": ticket_url}
        if mode == "reinit":
            have.update({"--name": name, "--slug": slug, "--email": email})
        given = {"--preset": a.preset, "--kb-name": a.kb_name, "--ticket-url": a.ticket_url,
                 "--name": a.name, "--slug": a.slug, "--email": a.email}
        for flag, val in given.items():
            if val is not None and flag in have and str(val) != str(have[flag]):
                print(f"init: WARNING {flag} '{val}' ignored: {existing.name} already has '{have[flag]}' "
                      f"(edit {existing.name} by hand to change it)")
        if a.slot:
            print(f"init: WARNING --slot ignored: the taxonomy in {existing.name} is fixed once set")

    remote = a.remote   # never asked: no --remote keeps the KB local (fix_remotes says how to add one)
    if remote:
        if not legacy() and is_tool_url(remote):
            sys.exit(f"init: --remote {remote} is the Leyden tool repo. Your KB needs its OWN private repo.")
        code, out = ls_remote(remote)
        if code != 0:
            sys.exit(f"init: cannot use --remote: {out}\n  Create the EMPTY private repo first (or check "
                     f"the URL and your access), then re-run. Nothing was written.")
        problem = remote_history_problem(remote, out)
        if problem:
            sys.exit(f"init: cannot use --remote: {problem}\n  Nothing was written.")

    target.mkdir(parents=True, exist_ok=True)
    if not (target / ".git").exists():
        subprocess.run(["git", "init", "-q", str(target)], check=True)

    cfg = {
        "kb": {"name": kb_name, "mode": "team" if mode in ("team-owner", "team-member") or existing_mode == "team" else "individual",
               "preset": preset},
        "engineer": {"name": name, "slug": slug, "email": email},
        "ticketing": {"provider": "linear" if "linear" in (ticket_url or "") else "generic", "url_base": ticket_url or ""},
    }

    report: list = []
    # ── KB .gitignore (runtime state, secret-shaped filenames, generated views)
    gi = KB / ".gitignore"
    if not gi.exists():
        write_if_absent(gi, KB_GITIGNORE, report)
    elif ".leyden/" not in gi.read_text(encoding="utf-8").splitlines():
        with gi.open("a", encoding="utf-8") as fh:
            fh.write("\n# Leyden runtime state (machine-local)\n.leyden/\n")
        report.append(("added", gi))
    (KB / ".leyden").mkdir(exist_ok=True)

    # ── record the KB for every hook and verb of this tool checkout
    kb_path_note = ""
    if not legacy():
        old = _config.KB_PATH_FILE.read_text(encoding="utf-8").strip() if _config.KB_PATH_FILE.is_file() else ""
        want = _config.kb_path_text(KB)   # relative to the workspace: survives mv and cp -R
        if old != want:
            _config.KB_PATH_FILE.write_text(want + "\n", encoding="utf-8")
            kb_path_note = f"  kb-path {_config.KB_PATH_FILE} -> {want}" + (f" (was {old})" if old else "")

    # ── write config
    if mode == "individual":
        text = emit_kb_block(cfg, T, include_taxonomy=(preset == "custom" or not preset_matches(T, preset)))
        text = text.replace("kb:\n", "engineer:\n" f"  name: {name}    # informational only\n  slug: {slug}\n"
                            f"  email: {email}\n\nkb:\n", 1)
        write_if_absent(KB / "leyden.yaml", text, report)
    elif mode == "team-owner":
        write_if_absent(KB / "leyden.yaml", emit_kb_block(cfg, T, include_taxonomy=True), report)
        write_if_absent(_config.LOCAL_CONFIG, emit_identity_block(cfg), report)
    elif mode == "team-member":
        write_if_absent(_config.LOCAL_CONFIG, emit_identity_block(cfg), report)

    # ── directories, rendered templates, seeds
    v = variables(T, cfg)
    D = _config.dirs({"kb": {"preset": preset}, "taxonomy": T})
    for d in (D["entity"], D["playbook"], Path(D["internal"]) / D["case"], D["projects"], RENDERED.name):
        (KB / d).mkdir(parents=True, exist_ok=True)
    for slot in _config.SLOTS:
        src = TEMPLATES / "src" / f"{slot}.md"
        write_if_absent(RENDERED / f"{T[slot]['singular']}.md", render(src.read_text(encoding="utf-8"), v), report)
    write_if_absent(RENDERED / "log.md", render((TEMPLATES / "seeds" / "entity-log.md").read_text(encoding="utf-8"), v), report)
    seeds = {
        "index.md": KB / "index.md",
        "open.md": KB / "open.md",
        "log.md": KB / "log.md",
        "entities-index.md": KB / D["entity"] / "index.md",
        "playbooks-index.md": KB / D["playbook"] / "index.md",
        "internal-index.md": KB / D["internal"] / "index.md",
        "projects-index.md": KB / D["projects"] / "index.md",
    }
    for seed, dest in seeds.items():
        write_if_absent(dest, render((TEMPLATES / "seeds" / seed).read_text(encoding="utf-8"), v), report)
    for d in (D["entity"], D["playbook"], Path(D["internal"]) / D["case"], D["projects"]):
        keep = KB / d / ".gitkeep"
        if not any((KB / d).iterdir()):
            keep.touch()

    # ── report
    print(f"\nLeyden: {kb_name}  ({cfg['kb']['mode']}, preset {preset})")
    if legacy():
        print(f"  KB    {KB}  (legacy: inside the tool checkout)")
    else:
        print(f"  KB    {KB}  (its own git repo)")
        print(f"  tool  {TOOL_ROOT}  (pull-only, no data)")
    if kb_path_note:
        print(kb_path_note)
    print("  " + "  ".join(f"{slot}={T[slot]['singular']}/{T[slot]['plural']}" for slot in _config.SLOTS))
    for action, path in report:
        print(f"  {action:<5} {path.relative_to(KB)}")

    for line in fix_remotes(remote):
        print(f"  remote: {line}")

    installed = None if a.write_hooks else hooks_installed()
    if a.write_hooks:
        print("  " + write_hooks(Path(a.write_hooks).expanduser()))
    elif installed:
        print(f"  hooks already registered in {installed}")   # the full JSON block on every re-run was noise
    elif recorded_settings() and recorded_settings().is_file():
        rs = recorded_settings()
        print(f"  hooks in {rs} differ from what this init writes (older init?): "
              f"update them with: leyden init --write-hooks {shlex.quote(str(rs))}")
    else:
        print("\nRegister the hooks in your workspace .claude/settings.json under \"hooks\" (or rerun with "
              "--write-hooks <path>, which also writes paths relative to $CLAUDE_PROJECT_DIR):")
        print(json.dumps(hook_json(), indent=2))

    # Workspace = the dir holding .claude/settings.json when hooks were written there, else the
    # tool's parent. Written between markers, so a re-run refreshes it and leaves the rest alone.
    ws = project_dir_for(Path(a.write_hooks)) if a.write_hooks else project_dir_for(recorded_settings())
    print("  " + write_claude_md(ws))

    if not a.no_doctor:
        print()
        # Flush first: piped, this process's buffered report used to land AFTER doctor's output.
        sys.stdout.flush()
        env = dict(os.environ)
        env["LEYDEN_HOME"] = str(KB)                       # doctor checks the KB init just wrote
        env["LDN_KB_SOURCE"] = "legacy" if legacy() else "kb-path"
        env.setdefault("LEYDEN_PYTHON", sys.executable)   # doctor checks the interpreter init ran on
        cmd = [str(TOOLS / "leyden-doctor.sh")]
        if a.write_hooks:
            cmd += ["--settings", str(Path(a.write_hooks).expanduser())]
        rc = subprocess.run(cmd, env=env).returncode
        if rc != 0:
            print("\ninit: the KB is written, but doctor reported FAIL lines above: the install is not "
                  "complete until they pass (re-check with `leyden doctor`).")
            return 1   # scripts must see the incomplete install, not exit 0
    return 0


# ── setup: the whole workspace in one non-interactive, idempotent command ──────────────────────────

SETUP_DOC = """\
leyden setup: set up Leyden for a workspace in one go, without questions. Idempotent: re-running
with the same arguments changes nothing and says so.

    leyden setup --personal <git-url>      your KB pushes to this EMPTY private repo
    leyden setup --personal new            create it first: gh repo create <slug>-kb --private
    leyden setup --personal local          no remote, the KB stays on this machine
    leyden setup ... --team <git-url>      also join a team repo (cloned into the workspace if needed)
    leyden setup ... --dry-run             print the plan and check prerequisites, change nothing

What it does: KB at <workspace>/<slug>-kb (slug from git config user.name, else user.email), the
three hooks in <workspace>/.claude/settings.json, the Leyden block in <workspace>/CLAUDE.md, the
skills linked into <workspace>/.claude/skills/, the team join, then `leyden doctor`. Exit non-zero
on any FAIL. Workspace default: the folder holding this tool checkout (never the current directory).
"""


def _git_at(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _abs_if_path(url: str) -> str:
    """A local path given relative to the caller's cwd becomes absolute (init and join resolve
    relative paths against other folders)."""
    if url and "://" not in url and _is_local_url(url):
        p = Path(url).expanduser()
        if p.exists():
            return str(p.resolve())
    return url


def _same_url(x: str, y: str) -> bool:
    def norm(u: str) -> str:
        u = u[7:] if u.startswith("file://") else u
        if "://" not in u and _is_local_url(u) and Path(u).expanduser().exists():
            u = str(Path(u).expanduser().resolve())
        return _norm_url(u)
    return bool(x) and bool(y) and norm(x) == norm(y)


def _setup_snapshot(ws: Path, kb: Path) -> list:
    """Everything setup can change, so a re-run can say truthfully that nothing changed."""
    snap = []
    for f in (ws / ".claude" / "settings.json", ws / "CLAUDE.md", _config.KB_PATH_FILE,
              kb / "leyden.yaml", kb / "leyden.local.yaml", kb / ".gitignore"):
        snap.append((str(f), f.read_bytes() if f.is_file() else None))
    sk = ws / ".claude" / "skills"
    snap.append(("skills", sorted((e.name, os.readlink(str(e)) if e.is_symlink() else "")
                                  for e in sk.iterdir()) if sk.is_dir() else []))
    if kb.is_dir():
        files = []
        for root, dirs, names in os.walk(kb):
            dirs[:] = [d for d in dirs if d not in (".git", ".leyden")]
            files += [os.path.relpath(os.path.join(root, n), str(kb)) for n in names]
        snap.append(("kb", sorted(files)))
        snap.append(("origin", _git_at(kb, "remote", "get-url", "origin")))
        snap.append(("hooks", _git_at(kb, "config", "--get", "leyden.hooksSettings")))
    return snap


def _gh_ready() -> str:
    """"" when gh is installed and logged in, else the reason (one line, with the fix)."""
    import shutil
    if not shutil.which("gh"):
        return ("the GitHub CLI (gh) is not installed, so `--personal new` cannot create the repo. "
                "Install it (brew install gh) and run `gh auth login`, or create an EMPTY private repo "
                "yourself and pass its URL: --personal <url>")
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        return ("gh is installed but not logged in. Run `gh auth login`, or create an EMPTY private "
                "repo yourself and pass its URL: --personal <url>")
    return ""


def _gh_repo_url(name: str) -> str:
    """The clone URL of <name> under the gh account (ssh or https per gh's git_protocol), "" if absent."""
    r = subprocess.run(["gh", "repo", "view", name, "--json", "url,sshUrl"], capture_output=True,
                       text=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        return ""
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return ""
    proto = subprocess.run(["gh", "config", "get", "git_protocol"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL).stdout.strip()
    if proto == "ssh" and d.get("sshUrl"):
        return d["sshUrl"]
    u = d.get("url") or ""
    return (u + ".git") if u and not u.endswith(".git") else u


def _team_joined(kb: Path, url: str) -> Path | None:
    """The team clone this KB is already connected to, when it is a clone of <url>."""
    use_kb(kb, "kb-path")
    sd = str(_config.get("team.shared_dir", "") or "").strip()
    if not sd:
        return None
    tp = Path(sd).expanduser()
    tp = tp if tp.is_absolute() else Path(os.path.normpath(str(kb / tp)))
    if not (tp / ".git").exists():
        return None
    origin = _git_at(tp, "remote", "get-url", "origin")
    return tp if (_same_url(origin, url) or _same_url(str(tp), url)) else None


def setup_main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="leyden setup", description=SETUP_DOC,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--personal", required=True, metavar="URL|new|local",
                    help="your KB's private remote: a git URL, `new` (gh repo create), or `local`")
    ap.add_argument("--team", metavar="URL", help="team repo to join (URL or local clone path)")
    ap.add_argument("--vocabulary", choices=list(_config.PRESETS), default=None,
                    help="words for folders and entries, chosen once for a NEW KB (default: deployment = "
                         "customer / deployment / issue / runbook)")
    ap.add_argument("--workspace", metavar="DIR", help="default: the folder holding this tool checkout")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and check prerequisites only")
    a = ap.parse_args(argv)

    ws = Path(a.workspace).expanduser().resolve() if a.workspace else TOOL_ROOT.parent.resolve()
    if inside(ws, TOOL_ROOT):
        sys.exit(f"leyden setup: the workspace cannot be the tool checkout ({TOOL_ROOT}) or inside it. "
                 f"Pass --workspace <the folder that holds leyden/>, e.g. --workspace {TOOL_ROOT.parent}")
    if not ws.is_dir():
        sys.exit(f"leyden setup: workspace {ws} does not exist")
    name, email = git_identity()
    if not name or not email:
        print("leyden setup: git user.name and user.email must be set first (every KB commit needs them):\n"
              '  git config --global user.name "Your Name"; git config --global user.email you@example.com',
              file=sys.stderr)
        return 2
    slug = derive_slug(name, email)
    kb = ws / f"{slug}-kb"
    served = _config.read_kb_path_file()
    if served is not None and served.is_dir() and inside(served, ws) and served.resolve() != kb.resolve():
        kb = served.resolve()   # this checkout already serves a KB in this workspace: keep it
    kb = kb.resolve()
    settings = ws / ".claude" / "settings.json"
    personal = a.personal.strip()
    team = _abs_if_path(a.team.strip()) if a.team else None
    has_git = (kb / ".git").exists()
    origin = _git_at(kb, "remote", "get-url", "origin") if has_git else ""

    # ── where the KB pushes
    remote, clone_from, notes = None, None, []
    if personal == "local":
        notes.append(f"remote: none (local only)" + (f"; existing origin {origin} kept" if origin else ""))
    elif personal == "new":
        repo_name = f"{slug}-kb"
        if origin:
            notes.append(f"remote: {origin} (already set, nothing created)")
        else:
            why = _gh_ready()
            if why:
                print(f"leyden setup: {why}", file=sys.stderr)
                return 1
            url = _gh_repo_url(repo_name)
            if url:
                notes.append(f"remote: {url} (GitHub repo {repo_name} already exists, reused)")
            elif a.dry_run:
                notes.append(f"remote: create private GitHub repo {repo_name} under your gh account")
            else:
                r = subprocess.run(["gh", "repo", "create", repo_name, "--private"], capture_output=True,
                                   text=True, stdin=subprocess.DEVNULL)
                if r.returncode != 0:
                    print(f"leyden setup: gh repo create {repo_name} --private failed:\n  "
                          f"{(r.stderr or r.stdout).strip()}", file=sys.stderr)
                    return 1
                url = _gh_repo_url(repo_name)
                if not url:
                    print(f"leyden setup: created {repo_name} but `gh repo view` cannot find it; "
                          f"re-run with --personal <its clone URL>", file=sys.stderr)
                    return 1
                notes.append(f"remote: {url} (created private GitHub repo {repo_name})")
            remote = url or None
            if url and not has_git and not kb.exists():
                code, out = ls_remote(url)
                if code == 0 and any("\t" in l for l in out.splitlines()):
                    clone_from, remote = url, None   # second laptop: the KB is already there
                    notes[-1] = f"remote: {url} (GitHub repo {repo_name} has commits: cloned as your KB)"
    else:
        url = _abs_if_path(personal)
        if is_tool_url(url):
            print(f"leyden setup: {url} is the Leyden tool repo, not a place for your KB. Give the URL of "
                  f"an EMPTY private repo you created for the KB (or `new` / `local`). Nothing was written.",
                  file=sys.stderr)
            return 1
        if origin and _same_url(origin, url):
            notes.append(f"remote: {origin} (already set)")
        elif origin:
            print(f"leyden setup: {kb} already pushes to {origin}, not {url}. Nothing changed. To re-point "
                  f"it on purpose: leyden init --kb {shlex.quote(str(kb))} --remote {shlex.quote(url)}",
                  file=sys.stderr)
            return 1
        else:
            use_kb(kb, "kb-path")
            code, out = ls_remote(url)
            if code != 0:
                print(f"leyden setup: cannot reach {out}\n  Create the EMPTY private repo first (or check "
                      f"the URL and your access), then re-run. Nothing was written.", file=sys.stderr)
                return 1
            heads = [l for l in out.splitlines() if "\t" in l]
            if heads and not kb.exists():
                clone_from = url   # an existing KB (a second laptop): clone it instead of starting empty
                notes.append(f"remote: {url} (has commits: cloned as your KB)")
            else:
                remote = url
                notes.append(f"remote: {url}")

    # ── vocabulary: chosen once, when the KB is created (the words become folder names)
    current = ""
    if has_git and (kb / "leyden.yaml").is_file():
        m = re.search(r"^\s*preset:\s*([\w-]+)", (kb / "leyden.yaml").read_text(encoding="utf-8"), re.M)
        current = m.group(1) if m else ""
    if team and (url_for_team := (remote or clone_from or origin)) and _same_url(team, url_for_team):
        print("leyden setup: --team is the same repo as your personal KB. The team repo is a separate, "
              "shared repo; give its URL, or leave --team out for solo. Nothing was written.", file=sys.stderr)
        return 1
    if clone_from:
        vocab_note = "as in the cloned KB (fixed for an existing KB)"
        if a.vocabulary:
            vocab_note += f"; --vocabulary {a.vocabulary} is not applied"
        vocab = None
    elif has_git and (kb / "leyden.yaml").is_file():
        if a.vocabulary and current and a.vocabulary != current:
            print(f"leyden setup: this KB already uses the {current} vocabulary; it is fixed once the KB "
                  f"exists (the words are folder names). Nothing changed.", file=sys.stderr)
            return 1
        vocab_note = f"{current or 'as in the existing KB'} (fixed for an existing KB)"
        vocab = None
    else:
        vocab = a.vocabulary or "deployment"
        T = _config.PRESETS[vocab]
        vocab_note = f"{vocab}: {T['entity'][0]} / {T['context'][0]} / {T['case'][0]} / {T['playbook'][0]}"

    why = settings_problem(settings)
    if why:
        print(f"leyden setup: {why} Nothing was written.", file=sys.stderr)
        return 1

    # ── plan
    print(f"leyden setup: workspace {ws}")
    print(f"  you      {name} <{email}>  (slug {slug})")
    print(f"  KB       {kb}" + ("  (exists)" if has_git else "  (new)"))
    print(f"  words    {vocab_note}")
    for n in notes:
        print(f"  {n}")
    print(f"  hooks    {settings}")
    print(f"  CLAUDE.md {ws / 'CLAUDE.md'} (Leyden block only)")
    print(f"  skills   {ws / '.claude' / 'skills'}/ (kb-sweep, leyden-setup)")
    joined = _team_joined(kb, team) if (team and has_git) else None
    if team:
        if joined:
            print(f"  team     {joined} (already joined)")
        else:
            print(f"  team     join {team} (an existing clone in the workspace is used, else it is cloned there)")
    if a.dry_run:
        import shutil
        if not shutil.which("claude"):
            print("  WARN     claude CLI not on PATH: capture and the sweep need it (doctor will FAIL)")
        print("dry run: nothing changed.")
        return 0

    before = _setup_snapshot(ws, kb)
    env = dict(os.environ, LEYDEN_PYTHON=os.environ.get("LEYDEN_PYTHON") or sys.executable)
    env.pop("LEYDEN_HOME", None); env.pop("LDN_KB_SOURCE", None)
    sys.stdout.flush()
    if clone_from:
        r = subprocess.run(["git", "clone", "-q", "--", clone_from, str(kb)], env=env)
        if r.returncode != 0:
            print(f"leyden setup: git clone {clone_from} failed", file=sys.stderr)
            return 1
        _git_at(kb, "config", "leyden.kbOrigin", clone_from)
    cmd = [sys.executable, str(TOOLS / "leyden-init.py"), "--kb", str(kb), "--write-hooks", str(settings),
           "--yes", "--no-doctor"]
    if remote:
        cmd += ["--remote", remote]
    if vocab:
        cmd += ["--preset", vocab]
    if subprocess.run(cmd, env=env).returncode != 0:
        if personal == "new":
            print("hint: for an https remote, `gh auth setup-git` lets git use your gh login", file=sys.stderr)
        print("leyden setup: init failed (see above); fix that and re-run the same command.", file=sys.stderr)
        return 1
    md = ws / "CLAUDE.md"
    md_text = md.read_text(encoding="utf-8") if md.is_file() else ""
    if MD_START not in md_text or MD_END not in md_text:
        print(f"leyden setup: the Leyden block is missing from {md} (see the CLAUDE.md line above); "
              "fix that and re-run the same command.", file=sys.stderr)
        return 1
    env["LEYDEN_HOME"], env["LDN_KB_SOURCE"] = str(kb), "kb-path"
    if team and not joined:
        sys.stdout.flush()
        r = subprocess.run([sys.executable, str(TOOLS / "leyden-team.py"), "join", team, "--yes",
                            "--workspace", str(ws)], env=env)
        if r.returncode != 0:
            print("leyden setup: team join failed (see above); fix that and re-run the same command.",
                  file=sys.stderr)
            return 1
    changed = _setup_snapshot(ws, kb) != before
    print()
    sys.stdout.flush()
    rc = subprocess.run([str(TOOLS / "leyden-doctor.sh"), "--settings", str(settings)], env=env).returncode
    print()
    if rc != 0:
        print("leyden setup: doctor reported FAIL lines above; fix them and re-run the same command.")
        return 1
    if changed:
        print(f"leyden setup: done. Restart Claude Code in {ws} so the hooks load.")
    else:
        print("leyden setup: already set up, nothing changed.")
    return 0


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["setup"]:
            sys.exit(setup_main(sys.argv[2:]))
        sys.exit(main())
    except (EOFError, KeyboardInterrupt):
        # every prompt runs before anything is created, so a cancelled init leaves nothing behind
        print("\ninit: cancelled at a prompt; nothing was written.", file=sys.stderr)
        sys.exit(130)
