#!/usr/bin/env python3
"""Shared transcript extractor for the leyden pipeline.

ONE parser, used by leyden-capture.sh (detection signals + the rendered transcript) and by
leyden-reconcile.sh (the backstop). They used to carry two hand-copied inline parsers that drifted
apart; the reconcile copy never got the KB-exclusion fix and consequently reported ~90% of entities
as "touched but not logged" every run.

Modes
  prose   user/assistant TEXT only, minus injected context. The entity/trigger signal.
  tools   tool_use inputs + tool_result bodies, minus anything that is the KB's OWN content.
  render  the readable, ordered rendering fed to the headless capture worker. Keeps EVERYTHING,
          but tags the KB's own reads/writes as [KB-INTERNAL] so the worker can tell "this session
          did work for entity X" apart from "this session read X's row in open.md".

Why `tools` must exclude the KB's own content
  The detected-entity hint list is built from tool content, because an entity's identity often
  lives ONLY in a tool payload (an email recipient, a written file). But tool_result bodies also
  contain whatever the assistant READ, and open.md names every open entity, log.md names all of
  them, and the lint output lists every file. Any session that so much as glanced at the KB was
  therefore reported as touching every entity at once, and the worker was told (capture-prompt.md:
  "treat it as a checklist, not a cap") to go hunting for work that was never done. A worker
  diagnosed this itself in capture.log: "The mention of <two entities> was purely from the injected
  dashboard context, not from actual session activity."

  Exclusion is deterministic, not heuristic: a tool_use whose INPUT targets the KB is dropped, and so
  is the tool_result carrying its answer (matched by tool_use_id). Content signatures catch the
  indirect routes: hook-injected context persisted to a tool-results file, or a subagent quoting the
  dashboard back.

  is-capture-worker
          no output; exit 0 if this transcript is a leyden capture run rather than a real session.
  has-new no output; exit 0 if a conversation record (user/assistant text or tool traffic, or a
          queued prompt) starts at or after --since-bytes, exit 3 if not (metadata-only growth: a
          session resumed and exited, a title or mode record appended). Any other status is an
          error and the caller must not treat it as "nothing new".
  emails  candidate email addresses, one per line: from conversation prose and from the recipient
          fields of mail-shaped tool calls only (never from arbitrary tool output, which is full of
          git@github.com and package-author addresses).

What is read
  - Only the ACTIVE branch of the conversation: the chain from the last record back through
    parentUuid (logicalParentUuid across a compaction). A rewind leaves the abandoned branch in the
    file; it never executed as far as the session is concerned and is not rendered as if it had.
  - Prompts typed while the model was busy (attachment records of type queued_command) count as
    user prose; task-notification payloads in the same shape do not.
  - Subagent transcripts (<session-dir>/subagents/agent-*.jsonl, siblings of the transcript) are
    included, bounded, because delegated work often holds the actual finding.

Usage
  _transcript.py <mode> <transcript.jsonl> [--kb DIR] [--since-bytes N]
                 [--entities REGEX --enum-threshold N]
  --since-bytes    where a resumed session's NEW work begins. render marks the boundary; prose,
                   tools and emails only emit records at/after it; has-new tests for it.
  --entities/--enum-threshold  drop any single block that names N+ distinct entities, which is a
                   list being echoed rather than work done for all of them (prose/tools modes).
                   `--customers` is accepted as the legacy spelling of --entities.

Directory and vocabulary names (customers/deployments/issues/runbooks, or whatever the KB is
configured with) come from leyden.yaml via _config.py; nothing here names a slot literally.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _config import dirs as _dirs, taxonomy as _taxonomy  # noqa: E402
    DIRS = _dirs()
    TAX = _taxonomy()
except Exception:  # broken config must not take the parser down with it
    DIRS = {"entity": "customers", "context": "deployments", "case": "issues",
            "playbook": "runbooks", "internal": "internal", "projects": "projects"}
    TAX = {"entity": {"singular": "customer", "plural": "customers"},
           "case": {"singular": "issue", "plural": "issues"}}

CASES = TAX["case"]["plural"]

CAP = 6000  # per tool block, to bound render size; conversation prose is kept in full
# Whole-render budget (characters). A 38 MB transcript used to render to 7.7 MB, which the worker
# cannot read in one go and mostly paid for as tool noise. Over budget, tool blocks shrink first,
# then the already-captured part above the NEW WORK boundary is elided, prose last.
try:
    RENDER_CAP = int(os.environ.get("LEYDEN_RENDER_CAP", "") or 600000)
except ValueError:
    RENDER_CAP = 600000
SUBAGENT_FILES_MAX = 20      # newest subagent transcripts read per session
SUBAGENT_CAP = 120000        # characters of subagent material in a render

# Text that only ever appears because the KB (or a leyden hook) put it there. Matched
# case-insensitively against tool inputs and tool results.
#
# Bare "leyden" rather than "leyden/" on purpose: the KB's own tooling is usually invoked with a
# RELATIVE path from the KB root ("python3 tools/leyden-lint.py"), so neither the absolute --kb path
# nor a trailing slash appears. That gap left every lint/dashboard/graph run and its output in the
# detection stream, which is where the residual false hits on three entities came from.
KB_SIGNATURES = (
    "leyden",
    "auto-maintained by the leyden capture hook",
    # open.md / closed.md / dashboard shapes, in case they are read by a relative path and the
    # frontmatter (which names leyden) falls outside a truncated result. Both the configured case
    # word and the legacy "issues" spelling, so a KB migrated from the old layout still matches.
    f"open {CASES} (dashboard)",
    "open issues (dashboard)",
    "read this first each session",
)

# The KB's own layout, referenced by a RELATIVE path (a find/grep/ls run with cwd=KB emits
# "./<entities>/<slug>/<cases>/foo.md", which contains neither "leyden" nor the absolute --kb path).
# Built from the configured directory names so a KB with accounts/environments/tickets/how-tos is
# filtered exactly like one with customers/deployments/issues/runbooks.
#
# The leading class deliberately EXCLUDES "-" so that a sibling repo whose path merely CONTAINS the
# entity dir name (say `ops/legacy-<entities>/<Name>/`) does NOT match: that is a different
# repo and a hit there is genuine entity evidence, not KB self-reference.
_E, _CTX, _CASE, _PB, _INT, _PRJ = (re.escape(DIRS[k]) for k in
                                     ("entity", "context", "case", "playbook", "internal", "projects"))
KB_PATH_RE = re.compile(
    r"""(?ix)
    (?:^|[\s"'`(\[,:=/])(?:\./)?
    (?:
        %(e)s/[a-z0-9-]+/(?:log|index)\.md
      | %(e)s/[a-z0-9-]+/(?:%(case)s|%(ctx)s)/
      | %(int)s/%(case)s/[a-z0-9._-]+\.md
      | %(pb)s/[a-z0-9._-]+\.md
      | %(prj)s/[a-z0-9._-]+\.md
      | _templates/
      | (?:open|closed)\.md
    )
    """ % {"e": _E, "case": _CASE, "ctx": _CTX, "int": _INT, "pb": _PB, "prj": _PRJ}
)

# Injected context arrives as a plain user turn (no <system-reminder> wrapper) whenever a hook
# returns additionalContext. Prose starting with one of these is the hook talking, not the user.
# The SessionStart header (leyden-load.sh) uses the configured case word; the legacy spelling is
# kept so transcripts recorded before a vocabulary change are still filtered.
INJECTED_PREFIXES = (
    f"# leyden open {CASES}",
    "# leyden open issues",
)
# The search hook's header names the KB (`# Relevant {kb_name} entries for this query: ...`), so a
# fixed "relevant leyden entries" string never matched a KB with any other name and every injected
# hit list was read back as user prose. Matched by SHAPE instead, whatever the KB is called.
# Current leyden-search.sh header: "# Possibly relevant <kb> entries (reference notes, ...":
# the old pattern only knew "# Relevant <kb> entries for this query", so hit lists read as prose.
INJECTED_RE = re.compile(r"^#\s*(?:possibly\s+)?relevant\s.{0,80}?\bentries\s+(?:for\s+this\s+query|\(reference\s+notes)",
                         re.I | re.M)
# Harness-generated user-turn content: slash-command plumbing, local command output, background task
# notifications. Typed by nobody, so never prose and never "new work".
HARNESS_PREFIXES = ("<local-command-", "<command-message>", "<task-notification>",
                    "<local-command-caveat>")
COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)


# A capture-worker run is itself a Claude Code session, so it lands in ~/.claude/projects/ alongside
# real ones. Its FIRST user message is capture-prompt.md. Detecting that matters because the worker's
# own prose enumerates the entity hint list ("Checked all other <entities> named in the hint list
# (a, b, ...)"), which the reconcile backstop then read back as evidence that all of those entities
# had been worked on. 17 of the 43 transcripts in a 3-day window were capture runs.
CAPTURE_WORKER_MARKER = "you are the leyden capture step"


def is_capture_worker(path):
    """True if this transcript is a leyden capture worker run rather than a real session."""
    for obj, _off in _records(path):
        if obj.get("type") != "user":
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            continue
        return CAPTURE_WORKER_MARKER in text.lower()
    return False


def _compile_entities(entities_re):
    """Compile the ERE alternation built by _guard.sh's ldn_entity_pattern.

    That pattern is `\\b(slug|sl[ -]ug|alias...)\\b` and the aliases come from leyden.yaml, i.e. from
    a human. One alias with a metacharacter (`c++corp`) made re.compile raise, which took the whole
    extractor down; the capture then saw empty prose, logged a skip and committed the offset, for
    every session. On a compile error the alternatives are re-escaped one by one (keeping the
    `[ -]` that stands for a space-or-dash in multi-word slugs) and compiled again. Returns None if
    even that fails; the caller then runs without the filter rather than not at all.
    """
    try:
        return re.compile(entities_re, re.I)
    except re.error:
        pass
    body = entities_re
    pre, suf = "\\b([A-Za-z0-9]+-)*(", ")(-[A-Za-z0-9]+)*\\b"
    if body.startswith(pre) and body.endswith(suf):
        body = body[len(pre):-len(suf)]
    elif body.startswith("\\b(") and body.endswith(")\\b"):
        body = body[3:-3]
    alts = []
    for piece in body.split("|"):
        if not piece:
            continue
        alts.append("[ -]".join(re.escape(part) for part in piece.split("[ -]")))
    if not alts:
        return None
    try:
        return re.compile(r"\b([A-Za-z0-9]+-)*((%s))(-[A-Za-z0-9]+)*\b" % "|".join(alts), re.I)
    except re.error:
        sys.stderr.write("_transcript.py: unusable --entities pattern, enumeration filter off\n")
        return None


def _enumeration_filter(entities_re, threshold):
    """Build a predicate that spots a block ENUMERATING entities rather than working on one.

    A single block naming four or more distinct entities is a list being echoed (the injected
    dashboard, a hint list quoted back, a lint report, an audit loop over every slug), not work done
    for four entities. Without this, one such sentence attributed the whole roster to the session.
    Returns a predicate that is always False when disabled, so the default behaviour is unchanged.
    """
    if not entities_re or threshold <= 0:
        return lambda _text: False
    pattern = _compile_entities(entities_re)
    if pattern is None:
        return lambda _text: False

    def is_enumeration(text):
        found = {m.group(0).lower().replace(" ", "-") for m in pattern.finditer(text)}
        return len(found) >= threshold

    return is_enumeration


# Tools that only ENUMERATE the filesystem. Their results are full of paths that happen to contain
# entity names ("scratch/<entity>-doc-DESIGN-HANDOFF.md" in an `ls`), which is evidence that a
# file exists, not that this session did work for that entity.
#
# The distinction is the TOOL, not the text: a path emitted by `Write` is exactly the artifact the
# reconcile backstop is meant to catch (an email drafted for an entity and never mentioned in
# prose), while the identical path emitted by `ls` is noise. So this filters by tool name and never
# by what the path looks like.
ENUMERATION_TOOLS = {"glob", "ls"}
ENUMERATION_CMD_RE = re.compile(r"^\s*(?:ls|find|tree|du|wc|stat|file|basename|dirname)\b", re.I)


def _is_enumeration_tool(name, input_blob):
    n = (name or "").lower()
    if n in ENUMERATION_TOOLS:
        return True
    if n == "bash":
        try:
            cmd = json.loads(input_blob).get("command", "")
        except (ValueError, AttributeError):
            cmd = input_blob
        return bool(ENUMERATION_CMD_RE.match(cmd or ""))
    return False


def _blob(x):
    """Serialize an arbitrary tool payload to one searchable string."""
    if isinstance(x, str):
        return x
    try:
        return json.dumps(x, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(x)


def _is_kb_content(text, kb):
    low = text.lower()
    if kb and kb.lower() in low:
        return True
    if any(sig in low for sig in KB_SIGNATURES):
        return True
    if INJECTED_RE.search(text.lstrip()):
        return True
    return bool(KB_PATH_RE.search(text))


def _flatten(x, out):
    if isinstance(x, str):
        out.append(x)
    elif isinstance(x, dict):
        out.append(_blob(x))
    elif isinstance(x, list):
        for i in x:
            _flatten(i, out)


def _records(path):
    """Yield (parsed_object, byte_offset_of_line_start). Byte offsets let render mode mark the
    resume boundary using the same units the capture consumer commits (file size)."""
    pos = 0
    with open(path, "rb") as fh:
        for raw in fh:
            start = pos
            pos += len(raw)
            try:
                obj = json.loads(raw.decode("utf-8", "ignore"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(obj, dict):
                yield obj, start


def _active_filter(path):
    """Predicate keep(obj, offset) that drops the turns ABANDONED by a rewind.

    A rewind (esc-esc, edit an earlier prompt) does not delete anything: the abandoned turns stay in
    the file and the new prompt is appended with a parentUuid pointing back to the fork point.
    Reading the file top to bottom therefore rendered work that was undone as if it had happened.
    The active branch is the chain from the LAST record back through parentUuid (a compaction
    boundary has parentUuid null and carries the link in logicalParentUuid, so the walk follows it).

    Being off that chain is NOT enough to drop a record: parallel tool calls hang their results off
    the chain as side branches (measured: 35 of 120 real transcripts, all tool_use/tool_result).
    A record is dropped only when the side branch it sits on STARTS with a typed user prompt, which
    is what a rewind leaves behind. Dangling links (a parent not in the file) keep the record:
    losing real history is worse than rendering a dead branch. Records without a uuid and sidechain
    records are always kept.
    """
    parent, is_prompt, leaf = {}, {}, None
    for obj, _off in _records(path):
        u = obj.get("uuid")
        if not u or obj.get("isSidechain"):
            continue
        parent[u] = obj.get("parentUuid") or obj.get("logicalParentUuid")
        content = (obj.get("message") or {}).get("content")
        is_prompt[u] = obj.get("type") == "user" and (
            isinstance(content, str)
            or (isinstance(content, list)
                and any(isinstance(c, dict) and c.get("type") == "text" for c in content)
                and not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)))
        leaf = u
    if leaf is None:
        return lambda _obj, _off: True
    chain, cur = set(), leaf
    while cur and cur not in chain and cur in parent:
        chain.add(cur)
        cur = parent[cur]
    memo = {}

    def abandoned(u):
        path_, cur_, result = [], u, False
        while True:
            if cur_ in memo:
                result = memo[cur_]
                break
            if cur_ in path_:          # a cycle in corrupt data: keep
                break
            path_.append(cur_)
            nxt = parent.get(cur_)
            if not nxt or nxt not in parent:
                break                  # dangling or root: keep
            if nxt in chain:
                result = is_prompt.get(cur_, False)
                break
            cur_ = nxt
        for p in path_:
            memo[p] = result
        return result

    def keep(obj, _off):
        u = obj.get("uuid")
        if not u or obj.get("isSidechain") or u in chain or u not in parent:
            return True
        return not abandoned(u)

    return keep


def _turns(path):
    """Yield (role, blocks, offset, kind, timestamp) for the conversation records on the active branch.

    kind is "turn", "queued" (a prompt typed while the model was busy: an attachment record of type
    queued_command, about a third of all prompts in a busy session, previously invisible to every
    mode) or "compact" (a compaction summary: a recap of earlier work, never new work).
    isMeta user records are harness plumbing (caveats, reminders) and are skipped.
    """
    keep = _active_filter(path)
    for obj, offset in _records(path):
        rtype = obj.get("type")
        ts = obj.get("timestamp") or ""
        if rtype == "attachment":
            att = obj.get("attachment") or {}
            if att.get("type") != "queued_command" or not keep(obj, offset):
                continue
            if att.get("commandMode") == "task-notification":
                continue
            prompt = att.get("prompt")
            if isinstance(prompt, str):
                blocks = [{"type": "text", "text": prompt}]
            elif isinstance(prompt, list):
                blocks = [b for b in prompt if isinstance(b, dict) and b.get("type") == "text"]
            else:
                continue
            yield "user", blocks, offset, "queued", ts
            continue
        if rtype not in ("user", "assistant") or obj.get("isMeta") or not keep(obj, offset):
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        role = msg.get("role", rtype)
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
        else:
            continue
        yield role, blocks, offset, ("compact" if obj.get("isCompactSummary") else "turn"), ts


TASK_NOTE_RE = re.compile(r"<task-notification>.*?(?:</task-notification>|\Z)", re.S)


def _prose_text(text):
    """One text block -> the prose worth keeping, or None when it is injected or harness content."""
    text = TASK_NOTE_RE.sub("", text or "")
    stripped = text.strip()
    if not stripped or stripped.startswith("<system-reminder"):
        return None
    low = stripped.lower()
    if any(low.startswith(p) for p in INJECTED_PREFIXES) or INJECTED_RE.match(stripped):
        return None
    if stripped.startswith("<command-"):
        # A slash command: only its arguments were typed by a person ("/exit" alone is not work,
        # which is what a resume-then-exit appends).
        args = " ".join(a.strip() for a in COMMAND_ARGS_RE.findall(stripped)).strip()
        if not args:
            return None
        name = COMMAND_NAME_RE.search(stripped)
        return "%s %s" % (name.group(1).strip() if name else "", args)
    if any(stripped.startswith(p) for p in HARNESS_PREFIXES):
        return None
    return text


def _prose_blocks(blocks):
    for b in blocks:
        if b.get("type") != "text":
            continue
        text = _prose_text(b.get("text"))
        if text is not None:
            yield text


def _subagent_paths(path):
    """Subagent transcripts of this session: <projects>/<proj>/<session-id>/subagents/agent-*.jsonl,
    i.e. a directory named after the transcript, next to it. Newest SUBAGENT_FILES_MAX only."""
    base = path[:-len(".jsonl")] if path.endswith(".jsonl") else path
    d = os.path.join(base, "subagents")
    try:
        names = [n for n in os.listdir(d) if n.startswith("agent-") and n.endswith(".jsonl")]
    except OSError:
        return []
    paths = [os.path.join(d, n) for n in names]
    paths.sort(key=lambda p: (os.path.getmtime(p), p))
    return paths[-SUBAGENT_FILES_MAX:]


def _since_timestamp(path, since_bytes):
    """Timestamp of the first record at/after since_bytes: "" = everything is new, None = nothing in
    the main transcript is new (so no subagent work can be new either). Subagent files have their own
    byte offsets, so their new/old split is by time."""
    if since_bytes <= 0:
        return ""
    for obj, off in _records(path):
        if off >= since_bytes and obj.get("timestamp"):
            return obj["timestamp"]
    return None


def _all_turns(path, since_bytes=0):
    """Main transcript then subagents: (role, blocks, kind, is_new, source). Subagent turns are only
    yielded when new (source is the agent file stem); main-transcript turns are all yielded, flagged,
    so tools mode can still pair an old tool_use id with its result."""
    for role, blocks, off, kind, _ts in _turns(path):
        yield role, blocks, kind, off >= since_bytes, ""
    since_ts = _since_timestamp(path, since_bytes)
    if since_ts is None:
        return
    for sp in _subagent_paths(path):
        name = os.path.basename(sp)[:-len(".jsonl")]
        for role, blocks, _off, kind, ts in _turns(sp):
            if since_ts and ts < since_ts:
                continue
            yield role, blocks, kind, True, name


def mode_prose(path, kb, is_enum=None, since_bytes=0):
    is_enum = is_enum or (lambda _t: False)
    for _role, blocks, kind, new, _src in _all_turns(path, since_bytes):
        if not new or kind == "compact":
            continue
        for text in _prose_blocks(blocks):
            if is_enum(text):
                continue
            print(text)


def mode_userprose(path, kb, is_enum=None, since_bytes=0):
    """What the USER typed or pasted (not Claude's replies, not tool output). Used for the
    new-customer signal: a customer named by the user before their folder exists."""
    is_enum = is_enum or (lambda _t: False)
    for role, blocks, kind, new, src in _all_turns(path, since_bytes):
        # src != "": a subagent transcript, whose "user" turn is the parent Claude's brief
        if not new or kind == "compact" or role != "user" or src:
            continue
        for text in _prose_blocks(blocks):
            if not is_enum(text):
                print(text)


def mode_tools(path, kb, is_enum=None, since_bytes=0):
    """tool_use inputs + tool_result bodies, with the KB's own content removed."""
    is_enum = is_enum or (lambda _t: False)
    kb_tool_ids = set()
    for _role, blocks, kind, new, _src in _all_turns(path, since_bytes):
        if kind == "compact":
            continue
        for b in blocks:
            btype = b.get("type")
            if btype == "tool_use":
                blob = _blob(b.get("input"))
                if _is_kb_content(blob, kb):
                    # Remember the id so the RESULT of this KB read/write is dropped too.
                    if b.get("id"):
                        kb_tool_ids.add(b["id"])
                    continue
                if is_enum(blob) or _is_enumeration_tool(b.get("name"), blob):
                    if b.get("id"):
                        kb_tool_ids.add(b["id"])
                    continue
                if not new:
                    continue
                parts = []
                _flatten(b.get("input"), parts)
                for p in parts:
                    print(p)
            elif btype == "tool_result":
                if not new or b.get("tool_use_id") in kb_tool_ids:
                    continue
                parts = []
                _flatten(b.get("content"), parts)
                for p in parts:
                    if not _is_kb_content(p, kb) and not is_enum(p):
                        print(p)


def mode_has_new(path, since_bytes):
    """True if a conversation record with real content starts at/after since_bytes. Growth made only
    of metadata (a resume that exits straight away, title/mode/snapshot records, a bare /exit) is
    not new work and must not buy another headless run."""
    for _role, blocks, off, kind, _ts in _turns(path):
        if off < since_bytes or kind == "compact":
            continue
        for b in blocks:
            btype = b.get("type")
            if btype in ("tool_use", "tool_result"):
                return True
            if btype == "text" and _prose_text(b.get("text")) is not None:
                return True
    return False


# Email candidates. Arbitrary tool OUTPUT is full of addresses nobody is working for (git@github.com
# in every clone URL, package authors in every pip/npm listing), and each one used to read as a
# "new customer" and buy a headless run. Only two places are trusted: what a person typed, and the
# recipient side of a tool call that sends or drafts mail.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAIL_TOOL_RE = re.compile(r"mail|outlook|draft|send_message|reply|forward", re.I)
RECIPIENT_KEYS = {"to", "cc", "bcc", "recipient", "recipients", "attendees", "email", "emails"}
HEADER_LINE_RE = re.compile(r"^[ \t]*(?:to|cc|bcc)[ \t]*:[ \t]*(.+)$", re.I | re.M)
IGNORED_LOCALPARTS = {"git", "noreply", "no-reply", "donotreply", "do-not-reply", "notifications",
                      "mailer-daemon", "postmaster"}


def _recipient_strings(x, out, under_key=False):
    if isinstance(x, dict):
        for k, v in x.items():
            _recipient_strings(v, out, under_key or str(k).lower() in RECIPIENT_KEYS)
    elif isinstance(x, list):
        for i in x:
            _recipient_strings(i, out, under_key)
    elif isinstance(x, str) and under_key:
        out.append(x)


def mode_emails(path, kb, since_bytes=0):
    found = []
    for _role, blocks, kind, new, _src in _all_turns(path, since_bytes):
        if not new or kind == "compact":
            continue
        for b in blocks:
            btype = b.get("type")
            if btype == "text":
                for text in _prose_blocks([b]):
                    found += EMAIL_RE.findall(text)
            elif btype == "tool_use":
                inp = b.get("input")
                if _is_kb_content(_blob(inp), kb):
                    continue
                vals = []
                if MAIL_TOOL_RE.search(b.get("name") or ""):
                    _recipient_strings(inp, vals)
                strs = []
                _flatten(inp, strs)
                for st in strs:
                    vals += HEADER_LINE_RE.findall(st)   # To:/Cc: lines of a drafted .eml/.md
                for v in vals:
                    found += EMAIL_RE.findall(v)
    seen = set()
    for addr in found:
        a = addr.lower().strip(".")
        if a in seen or a.split("@", 1)[0] in IGNORED_LOCALPARTS:
            continue
        seen.add(a)
        print(a)


# ── render ────────────────────────────────────────────────────────────────────────────────────────
NEW_WORK_MARKER = ("\n================ NEW WORK BELOW (everything above was captured in an "
                   "earlier pass: do not re-log it) ================")
KB_TOOL_TAG = " [KB-INTERNAL: leyden's own file, NOT session evidence]"
KB_RESULT_TAG = " [KB-INTERNAL: leyden's own content, NOT session evidence]"


def _render_input(x):
    """A tool_use input as readable lines (`key: value`, multi-line strings verbatim), not one
    escaped JSON string where every newline of a heredoc or an email body shows up as \\n."""
    if not isinstance(x, dict):
        return _blob(x)
    lines = []
    for k, v in x.items():
        if isinstance(v, str):
            lines.append("%s:%s%s" % (k, "\n" if "\n" in v else " ", v))
        else:
            lines.append("%s: %s" % (k, _blob(v)))
    return "\n".join(lines)


def _result_text(content):
    """A tool_result body as text: text blocks verbatim, images as a placeholder."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                out.append(c.get("text") or "")
            elif isinstance(c, dict) and c.get("type") == "image":
                out.append("[image]")
            else:
                out.append(_blob(c))
        return "\n".join(out)
    return _blob(content)


def _render_blocks(segs, blocks, label, kb, kb_tool_ids, new):
    """Append render segments [kind, header, body, is_new] for one record's blocks."""
    for b in blocks:
        btype = b.get("type")
        if btype == "text":
            for text in _prose_blocks([b]):
                segs.append(["prose", "\n[%s]\n" % label, text, new])
        elif btype == "tool_use":
            tag = ""
            if _is_kb_content(_blob(b.get("input")), kb):
                tag = KB_TOOL_TAG
                if b.get("id"):
                    kb_tool_ids.add(b["id"])
            segs.append(["tool", "\n[%s -> tool: %s%s]\n" % (label, b.get("name", ""), tag),
                         _render_input(b.get("input")), new])
        elif btype == "tool_result":
            body = _result_text(b.get("content"))
            tag = ""
            if b.get("tool_use_id") in kb_tool_ids or _is_kb_content(body, kb):
                tag = KB_RESULT_TAG
            err = " (error)" if b.get("is_error") else ""
            segs.append(["tool", "\n[TOOL RESULT%s%s]\n" % (err, tag), body, new])


def mode_render(path, kb, since_bytes=0):
    """Ordered readable rendering. Nothing is dropped: the worker needs to see what was already
    written to the KB so it updates in place instead of duplicating. KB-owned tool traffic is
    TAGGED instead, so 'read open.md' can't be mistaken for 'did work for these entities'."""
    segs = []
    kb_tool_ids = set()
    boundary_done = since_bytes <= 0
    for role, blocks, offset, kind, _ts in _turns(path):
        if not boundary_done and offset >= since_bytes:
            segs.append(["marker", NEW_WORK_MARKER, "", True])
            boundary_done = True
        if kind == "compact":
            # A compaction summary is a recap of turns that are ALSO earlier in this file. Below the
            # boundary it must not read as new work, so it is labelled and treated as old context.
            text = "\n".join(t for t in (b.get("text") or "" for b in blocks
                                         if b.get("type") == "text") if t)
            segs.append(["prose", "\n[COMPACTION SUMMARY: recap of EARLIER work in this session; "
                         "prior context only, NOT new work]\n", text, False])
            continue
        label = role.upper() + (" (typed while the assistant was busy)" if kind == "queued" else "")
        _render_blocks(segs, blocks, label, kb, kb_tool_ids, boundary_done)
    if not boundary_done and since_bytes > 0:
        # Offset was at or past EOF: everything is old. Say so rather than silently rendering it
        # as if it were new.
        segs.append(["marker", "\n================ NOTHING NEW SINCE THE LAST CAPTURE "
                     "================", "", True])
    sub = _render_subagents(path, kb, _since_timestamp(path, since_bytes))
    if sub:
        segs.append(["marker", sub, "", True])
    sys.stdout.write(_fit(segs, RENDER_CAP))


def _render_subagents(path, kb, since_ts):
    """Work delegated to subagents, rendered after the main conversation, bounded to SUBAGENT_CAP."""
    if since_ts is None:
        return ""
    segs = []
    for sp in _subagent_paths(path):
        name = os.path.basename(sp)[:-len(".jsonl")]
        part, ids = [], set()
        for role, blocks, _off, kind, ts in _turns(sp):
            if (since_ts and ts < since_ts) or kind == "compact":
                continue
            _render_blocks(part, blocks, "SUBAGENT %s: %s" % (name, role.upper()), kb, ids, True)
        if part:
            segs.append(["marker", "\n---- subagent %s ----" % name, "", True])
            segs += part
    if not segs:
        return ""
    return ("\n================ SUBAGENT TRANSCRIPTS (work this session delegated; same session, "
            "bounded) ================" + _fit(segs, SUBAGENT_CAP))


def _trunc(s, cap=CAP):
    if not isinstance(s, str):
        s = _blob(s)
    if len(s) <= cap:
        return s
    if cap <= 0:
        return "[%d chars omitted: render size cap]" % len(s)
    return s[:cap] + f"\n...[truncated {len(s) - cap} chars]"


def _join(segs, tool_cap):
    return "".join(h + (_trunc(b, tool_cap) if k == "tool" else b) + "\n" for k, h, b, _n in segs)


def _fit(segs, cap):
    """Join segments within `cap` characters: shrink tool blocks first, then drop already-captured
    (old) segments oldest first, then cut the middle. Prose of the new work goes last."""
    text = ""
    for tool_cap in (CAP, 2000, 800, 300, 0):
        text = _join(segs, tool_cap)
        if len(text) <= cap:
            return text
    sizes = [len(h) + len(_trunc(b, 0) if k == "tool" else b) + 1 for k, h, b, _n in segs]
    total, drop = len(text), set()
    for i, seg in enumerate(segs):
        if total <= cap:
            break
        if not seg[3]:
            drop.add(i)
            total -= sizes[i]
    if drop:
        kept, noted = [], False
        for i, seg in enumerate(segs):
            if i in drop:
                if not noted:
                    kept.append(["marker", "\n...[%d already-captured blocks elided: render size "
                                 "cap]" % len(drop), "", True])
                    noted = True
                continue
            kept.append(seg)
        text = _join(kept, 0)
    if len(text) > cap:
        head = cap // 5
        tail = max(cap - head - 200, 0)
        text = (text[:head] + "\n...[%d chars elided from the middle: render size cap]...\n"
                % (len(text) - head - tail) + (text[-tail:] if tail else ""))
    return text


def main(argv):
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        return 2
    mode, path = argv[1], argv[2]
    kb = ""
    since = 0
    entities_re = ""
    enum_threshold = 0
    rest = argv[3:]
    i = 0
    while i < len(rest):
        if rest[i] == "--kb" and i + 1 < len(rest):
            kb = os.path.abspath(rest[i + 1]).rstrip("/")
            i += 2
        elif rest[i] == "--since-bytes" and i + 1 < len(rest):
            try:
                since = int(rest[i + 1])
            except ValueError:
                since = 0
            i += 2
        elif rest[i] in ("--entities", "--customers") and i + 1 < len(rest):
            entities_re = rest[i + 1]
            i += 2
        elif rest[i] == "--enum-threshold" and i + 1 < len(rest):
            try:
                enum_threshold = int(rest[i + 1])
            except ValueError:
                enum_threshold = 0
            i += 2
        else:
            i += 1
    if not os.path.isfile(path):
        return 1
    is_enum = _enumeration_filter(entities_re, enum_threshold)
    if mode == "prose":
        mode_prose(path, kb, is_enum, since)
    elif mode == "userprose":
        mode_userprose(path, kb, is_enum, since)
    elif mode == "tools":
        mode_tools(path, kb, is_enum, since)
    elif mode == "emails":
        mode_emails(path, kb, since)
    elif mode == "render":
        mode_render(path, kb, since)
    elif mode == "has-new":
        # 0 = new conversation content, 3 = metadata-only growth. 1/2 stay "error" so a crash or a
        # missing file is never mistaken for "nothing new" (which would commit the offset).
        return 0 if mode_has_new(path, since) else 3
    elif mode == "is-capture-worker":
        # Exit status is the answer: 0 = yes (skip it), 1 = no. Nothing is printed.
        return 0 if is_capture_worker(path) else 1
    else:
        sys.stderr.write(f"unknown mode: {mode}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
