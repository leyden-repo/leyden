#!/usr/bin/env python3
"""
leyden remind: dated reminders that surface at the start of the next Claude session.

    leyden remind add <when> <text...> [--ref PATH] [--slack ID]
    leyden remind list [--all]          open reminders (--all: include done)
    leyden remind due                   due now + next 24h (what the SessionStart hook injects)
    leyden remind done <id>             tick it off
    leyden remind snooze <id> <when>    move the due time
    leyden remind rm <id>               delete the line

<when> accepts: 2026-11-05 | 2026-11-05 14:30 | 2026-11-05T14:30 | today 16:00 | tomorrow |
                tomorrow 10:00 | mon..sun or monday..sunday [HH:MM] (next occurrence) |
                +3d | +2h | +30m
A date without a time means 09:00 local. Times are local and stored with their UTC offset.

Storage is reminders.md at the KB root, one checklist line per reminder, hand-editable:

    - [ ] 2030-01-15T10:00+00:00 r7f3a acme: send the follow-up summary  (ref: customers/acme/... slack: <msg-id>)

Delivery is a Slack self-DM. The tool cannot call Slack itself, so the flow (Claude does it) is:
  1. slack_schedule_message to your own Slack user id at the due time (ping text = the reminder + refs)
  2. leyden remind add <when> <text> --slack <scheduled message id>
reminders.md is the record (list/done/snooze); the SessionStart injection of `due` is a backstop
for a missed DM. When to set one: when the user asks, or unasked only for IMPORTANT dated follow-ups
(a revert, a promised customer reply, an expiring credential), never for routine noise.

reminders.md is a personal file: lint/index treat it as repo documentation (not KB content) and the
team share never copies it.

stdlib only, python 3.9+.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config as _cfgmod  # noqa: E402

NAME = "leyden remind"
HEADER = (
    "# Reminders\n\n"
    "Managed by `leyden remind` (hand edits are fine, keep the line shape).\n"
    "Due and next-24h reminders are injected at SessionStart.\n\n"
)
LINE_RE = re.compile(
    r"^- \[(?P<done>[ xX])\] (?P<due>\d{4}-\d\d-\d\dT\d\d:\d\d(?:[+-]\d\d:\d\d)?) "
    r"(?P<id>r[0-9a-f]{4,8}) (?P<rest>.*)$"
)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DEFAULT_TIME = (9, 0)
HHMM_RE = re.compile(r"(?:\d{1,2}:\d\d|\d{1,2})(?:am|pm)?|\d{1,2}:\d\d", re.I)   # 10:30, 10, 10am, 3pm


def kb_file() -> Path:
    # The KB comes from _config (LEYDEN_HOME, .kb-path); parent.parent is the tool checkout here.
    return _cfgmod.require_kb() / "reminders.md"


def die(msg: str, code: int = 2) -> None:
    sys.stderr.write(f"{NAME}: {msg}\n")
    sys.exit(code)


def now() -> datetime:
    return datetime.now().astimezone().replace(second=0, microsecond=0)


def _hhmm(s: Optional[str]) -> Tuple[int, int]:
    if not s:
        return DEFAULT_TIME
    m = re.fullmatch(r"(\d{1,2})(?::(\d\d))?\s*(am|pm)?", s.strip().lower())
    if not m:
        die(f"bad time '{s}', use HH:MM")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if not 1 <= h <= 12:
            die(f"bad time '{s}'")
        h = h % 12 + (12 if ap == "pm" else 0)
    if h > 23 or mi > 59:
        die(f"bad time '{s}', use HH:MM")
    return h, mi


def _day_index(w: str) -> Optional[int]:
    # Full or 3-letter names only: a prefix match read "sunset" or "monitor" as a weekday.
    if w in DAYS:
        return DAYS.index(w)
    if w in DAY_NAMES:
        return DAY_NAMES.index(w)
    return None


def _local(day: datetime, h: int, mi: int) -> datetime:
    # Wall-clock time -> epoch via mktime (tm_isdst=-1 lets the C library pick the offset in force
    # THAT day), then back. A time that does not exist (the spring-forward hour) lands just after
    # the jump instead of an hour early; an ambiguous one (fall-back) takes the first occurrence.
    import time as _t
    ts = _t.mktime((day.year, day.month, day.day, h, mi, 0, 0, 0, -1))
    dt = datetime.fromtimestamp(ts).astimezone()
    if (dt.hour, dt.minute) < (h, mi) and dt.date() == day.date():
        dt = datetime.fromtimestamp(ts + 3600).astimezone()   # libc resolved the gap backwards
    return dt


def parse_when(words: List[str]) -> datetime:
    """Parse <when> from 1 or 2 words. Returns an aware local datetime."""
    if not words:
        die("missing <when>")
    w = words[0].lower()
    t = words[1] if len(words) > 1 else None
    base = now()
    m = re.fullmatch(r"\+(\d+)([mhd])", w)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n > 3660 * (1 if unit == "d" else 24 if unit == "h" else 1440):
            die(f"'{w}' is too far ahead (10 years at most)")
        if unit == "d":
            return _local(base + timedelta(days=n), base.hour, base.minute)
        return base + (timedelta(minutes=n) if unit == "m" else timedelta(hours=n))
    if w in ("today", "tomorrow"):
        h, mi = _hhmm(t)
        return _local(base + timedelta(days=1 if w == "tomorrow" else 0), h, mi)
    di = _day_index(w)
    if di is not None:
        h, mi = _hhmm(t)
        ahead = (di - base.weekday()) % 7
        cand = _local(base + timedelta(days=ahead), h, mi)
        return cand if cand > base else _local(base + timedelta(days=ahead + 7), h, mi)
    # [Tt ]: the input is lowercased above, so "2026-11-05T14:30" arrives as "...t14:30".
    m = re.fullmatch(r"(\d{4}-\d\d-\d\d)(?:[Tt ](\d{1,2}:\d\d))?", w)
    if m:
        h, mi = _hhmm(m.group(2) or t)
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            die(f"bad date '{m.group(1)}'")
        return _local(d, h, mi)
    die(f"can't read <when> '{' '.join(words)}' (see --help)")
    raise AssertionError


def when_arity(args: List[str]) -> int:
    """How many leading words of args make up <when>: 2 if the second word is an HH:MM and the first
    takes a time (today/tomorrow/weekday/bare date). `+2h 10:00 call` keeps 10:00 in the text."""
    w = args[0].lower() if args else ""
    if not (w in ("today", "tomorrow") or _day_index(w) is not None or re.fullmatch(r"\d{4}-\d\d-\d\d", w)):
        return 1
    # "tomorrow at 10 ...": drop the "at" so the time is the second word
    if len(args) > 2 and args[1].lower() == "at" and HHMM_RE.fullmatch(args[2]):
        del args[1]
    if len(args) > 1 and HHMM_RE.fullmatch(args[1]):
        return 2
    return 1


def fmt(d: datetime) -> str:
    return d.isoformat(timespec="minutes")


def load(warn: bool = False) -> Tuple[List[str], List[dict]]:
    f = kb_file()
    if not f.exists():
        return [], []
    lines = f.read_text(encoding="utf-8").splitlines()
    items = []
    for i, ln in enumerate(lines):
        m = LINE_RE.match(ln)
        due = None
        if m:
            try:
                due = datetime.fromisoformat(m.group("due"))
            except ValueError:
                due = None
        if due is None:
            if warn and ln.startswith("- ["):   # a hand edit that broke the shape: say so, never drop silently
                sys.stderr.write(f"{NAME}: warning: {f.name}:{i + 1} is not a reminder line, ignored: {ln}\n")
            continue
        if due.tzinfo is None:
            due = due.astimezone()
        items.append({"i": i, "done": m.group("done") != " ", "due": due,
                      "id": m.group("id"), "rest": m.group("rest")})
    return lines, items


@contextmanager
def locked():
    """Serialise load -> save across processes: two parallel adds used to lose a line."""
    kb_file()
    lock = _cfgmod.STATE / "reminders.lock"   # .leyden/ is gitignored, so the lock never gets pushed
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def save(lines: List[str]) -> None:
    f = kb_file()
    while lines and not lines[0].strip():   # a blank first line used to get a second "# Reminders" header
        lines = lines[1:]
    body = "\n".join(lines).rstrip("\n") + "\n"
    if not body.startswith("# "):
        body = HEADER + body
    tmp = f.with_name(f".reminders.md.{os.getpid()}.tmp")   # per-pid: a shared tmp name crashed parallel writers
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(f)


def render(it: dict, ref_now: datetime) -> str:
    delta = it["due"] - ref_now
    mins = int(delta.total_seconds() // 60)
    if it["done"]:
        tag = "done"
    elif mins < 0:
        a = -mins
        tag = f"OVERDUE {a // 1440}d" if a >= 1440 else (f"OVERDUE {a // 60}h" if a >= 60 else "DUE")
    elif mins < 1440:
        tag = f"in {mins // 60}h{mins % 60:02d}m"
    else:
        tag = f"in {mins // 1440}d"
    local = it["due"].astimezone().strftime("%a %d %b %H:%M")
    return f"- {it['id']} [{tag}] {local}: {it['rest']}"


def find(items: List[dict], rid: str) -> dict:
    for it in items:
        if it["id"] == rid:
            return it
    die(f"no reminder with id '{rid}' (run: list --all)", 1)
    raise AssertionError


def main(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0 if argv else 2
    cmd, args = argv[0], argv[1:]

    if cmd == "add":
        ref = slack = None
        rest = []
        k = 0
        while k < len(args):
            if args[k] in ("--ref", "--slack") and k + 1 < len(args):
                if args[k] == "--ref":
                    ref = args[k + 1]
                else:
                    slack = args[k + 1]
                k += 2
            else:
                rest.append(args[k])
                k += 1
        n = when_arity(rest)
        due = parse_when(rest[:n])
        text = " ".join(rest[n:]).strip()
        if not text:
            die("missing <text>")
        text = text.replace("\n", " ")
        extra = []
        if ref:
            extra.append(f"ref: {ref}")
        if slack:
            extra.append(f"slack: {slack}")
        if extra:
            text += "  (" + " ".join(extra) + ")"
        with locked():
            lines, items = load()
            used = {it["id"] for it in items}
            rid = "r" + secrets.token_hex(2)
            while rid in used:
                rid = "r" + secrets.token_hex(2)
            if not any(ln.strip() for ln in lines):
                lines = HEADER.splitlines()
            lines.append(f"- [ ] {fmt(due)} {rid} {text}")
            save(lines)
        if due <= now():
            print(f"note: {fmt(due)} is not in the future, it will show as due right away", file=sys.stderr)
        print(f"added {rid} for {due.strftime('%a %d %b %H:%M %Z')}: {text}")
        return 0

    if cmd in ("list", "ls"):
        _, items = load(warn=True)
        show_all = "--all" in args
        sel = [it for it in items if show_all or not it["done"]]
        if not sel:
            print("no reminders")
            return 0
        n = now()
        for it in sorted(sel, key=lambda x: x["due"]):
            print(render(it, n))
        return 0

    if cmd == "due":
        # Silent when nothing is due or upcoming: the SessionStart hook adds nothing in that case.
        if _cfgmod.KB_SOURCE == "none":
            return 0
        _, items = load(warn=True)
        n = now()
        horizon = n + timedelta(hours=24)
        sel = sorted((it for it in items if not it["done"] and it["due"] <= horizon), key=lambda x: x["due"])
        if not sel:
            return 0
        print("## Reminders (due now or within 24h)\n")
        for it in sel:
            print(render(it, n))
        print("\nRaise the DUE/OVERDUE ones with the user at the start of this session. "
              "Close with `leyden remind done <id>`, or `snooze <id> <when>`.")
        return 0

    if cmd in ("done", "rm", "snooze"):
        if not args:
            die(f"usage: {cmd} <id>" + (" <when>" if cmd == "snooze" else ""))
        if cmd == "snooze" and len(args) < 2:
            die("usage: snooze <id> <when>")
        due = parse_when(args[1:3]) if cmd == "snooze" else None
        with locked():
            lines, items = load()
            it = find(items, args[0])
            if cmd == "done":
                if it["done"]:
                    print(f"already done {it['id']}: {it['rest']}")
                    return 0
                lines[it["i"]] = lines[it["i"]].replace("- [ ]", "- [x]", 1)
                msg = f"done {it['id']}: {it['rest']}"
            elif cmd == "rm":
                del lines[it["i"]]
                msg = f"removed {it['id']}: {it['rest']}"
            else:
                if it["done"]:
                    die(f"{it['id']} is already done; add a new reminder instead")
                lines[it["i"]] = f"- [ ] {fmt(due)} {it['id']} {it['rest']}"
                msg = f"snoozed {it['id']} to {due.strftime('%a %d %b %H:%M')}"
            save(lines)
        print(msg)
        if it["rest"].find("slack: ") >= 0 and cmd != "done":
            print("note: this reminder has a scheduled Slack DM; cancel/move it in Slack 'Drafts and sent'",
                  file=sys.stderr)
        return 0

    die(f"unknown command '{cmd}' (see --help)")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
