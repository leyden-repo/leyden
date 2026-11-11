# leyden/tools: capture + retrieval hooks

Three Claude Code hooks keep the KB current with near-zero manual effort. Names below use the
generic slot words (entity/context/case/playbook); your KB may show different ones depending on
its preset (`tools/_config.py taxonomy`).

## Registered hooks

| Event | Script | What it does |
|-------|--------|---------------|
| SessionStart | `leyden-load.sh` | Injects the brief open-cases digest (one line per open case via `leyden-issues.py --brief`, most recently touched first, capped at about 2 KB of case lines, `LEYDEN_DIGEST_BYTES`, plus an optional line cap `LEYDEN_DIGEST_MAX`; falls back to the full `open.md`), plus one health line only when something is wrong (`_health.sh`), the sweep nudge and due reminders (`leyden-remind.py due`); drains the capture queue in the background; kicks the batched push, the daily metrics line, the weekly reconcile, and (when a team repo is connected) a background refresh of the team snapshot at most every 6 hours. All of this goes to Claude as `additionalContext`; a visible `systemMessage` appears only for a health problem, a due reminder or a sweep report waiting for review. It never shares anything: sharing is done by Claude when the user asks |
| UserPromptSubmit | `leyden-search.sh` | BM25 search over the FTS5 index with a frecency boost (no LLM), injects up to 5 entries that clear the relevance floor (nothing for a prompt with fewer than two content words, unless that word is a ticket id, error code or entity name); falls back to the original grep scan; logs every query's hits/misses |
| SessionEnd | `leyden-enqueue.sh` | Appends the ended transcript's path to `capture-queue.txt` and nothing else |

`.claude/settings.json` (project scope), as `leyden init --write-hooks <file>` writes it for the tool
checked out at `<project>/leyden`. The hooks run from the tool checkout and find the KB through
`leyden/.kb-path` (written by init; `LEYDEN_HOME` overrides it), resolved by `tools/_kb.sh` for the
shell tools and `tools/_config.py` for the python ones. With no KB configured every hook exits 0
silently. All runtime state goes to `<KB>/.leyden/` (gitignored); nothing is written into the tool
checkout:
```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "LEYDEN_HOME=\"$CLAUDE_PROJECT_DIR\"/me-kb LDN_KB_SOURCE=hook \"$CLAUDE_PROJECT_DIR\"/leyden/tools/leyden-load.sh" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "LEYDEN_HOME=\"$CLAUDE_PROJECT_DIR\"/me-kb LDN_KB_SOURCE=hook \"$CLAUDE_PROJECT_DIR\"/leyden/tools/leyden-search.sh" }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "LEYDEN_HOME=\"$CLAUDE_PROJECT_DIR\"/me-kb LDN_KB_SOURCE=hook \"$CLAUDE_PROJECT_DIR\"/leyden/tools/leyden-enqueue.sh" }] }]
  }
}
```
Each command carries its KB relative to the project (`LEYDEN_HOME="$CLAUDE_PROJECT_DIR"/<kb>`), so moving or copying the workspace keeps working, and two workspaces that share one tool clone
never cross-wire: `.kb-path` is only the default for commands run by hand, and re-pointing it at a
different KB needs `leyden init --force`. `leyden doctor` flags a hook that serves another KB.
The command is run by a shell, so paths are quoted (a path with a space in it otherwise fails
every hook with exit 126). A KB outside the project, or a user-level `~/.claude/settings.json`,
gets the absolute path, single-quoted. Re-running `--write-hooks` updates an existing Leyden
registration in place (matched by script name), so a moved clone never ends up registered twice.

## Why capture is deferred, not immediate

SessionEnd fires while the session is terminating, so it cannot reliably run a job that takes
minutes: Claude Code may kill child processes. So SessionEnd only *enqueues* a path. The **next**
SessionStart drains the queue in the background, where the session stays alive long enough. Net
effect: one capture per session, off the critical path, one session behind.

## `leyden-capture.sh`: the capture worker

Not a hook itself. Invoked per queued transcript by the drain, with `LEYDEN_BG=1` and
`LEYDEN_TRANSCRIPT` set.

1. **Recursion guard**: `LEYDEN_CAPTURE` stops the headless child re-triggering the whole pipeline.
2. **Offset dedup**: the transcript path is canonicalised (`ldn_realpath`) before every offset,
   retry and queue operation, so `./x`, an absolute path and `/tmp` vs `/private/tmp` are one key.
   Skip unless the transcript grew past the committed offset, and skip (committing the offset) when
   the growth holds no new conversation record (`_transcript.py has-new`, exit 3): a resume-then-exit
   only appends metadata and must not buy another headless run.
3. **Pre-filter**: over the NEW bytes only (`--since-bytes`), capture runs only if one of these
   fires: a known entity (folder name or registry alias) in the conversation prose (including
   prompts typed while the model was busy, `queued_command`, and subagent transcripts under
   `<session>/subagents/`) or in non-KB tool payloads; a ticket id with a prefix listed in
   `ticketing.prefixes` in `leyden.yaml` (exact match); an email domain that is not your own,
   taken from prose or mail-tool recipients only (freemail, `git@`, noreply and code-host addresses
   are ignored, plus `capture.ignore_email_domains`); or the USER typing the entity word
   ("customer", "customers") as a whole word (the new-entity signal: the worker creates the folder when
   it finds a real name, and writes nothing when it does not). Generic trigger phrases ("root
   cause", "next step") and ticket-shaped ids without a configured prefix do not count. A failing
   extractor requeues; it never counts as "nothing happened". A RESUMED session whose new part
   names nothing still runs when the earlier part of the same transcript touched an entity (the
   "it's fixed now, log it" follow-up).
4. **Render**: `_transcript.py render` turns the JSONL into ordered readable text: rewind branches
   dropped, compaction summaries tagged as prior context, tool I/O as plain text, total capped at
   `LEYDEN_RENDER_CAP` (default 600 KB; tool output is cut first).
5. **Headless capture**: `claude -p --model sonnet` (`capture.model`, default `sonnet`; no daily cost cap;
   `capture.max_turns` is passed only if your Claude Code offers `--max-turns`, which 2.1.318 does not) under the contract in
   `capture-prompt.md`. The worker runs with cwd = the KB, `--permission-mode dontAsk`, a generated
   `--settings` file allowing only `Read`, `Grep`, `Glob` and `Edit`/`Write` under the KB (shell,
   web and agents denied), `--setting-sources user` (project settings of the workspace are not
   loaded; `LEYDEN_CAPTURE_SETTING_SOURCES` overrides) and `--strict-mcp-config` (no MCP). The
   prompt goes in on stdin. A watchdog kills it after `LEYDEN_CAPTURE_TIMEOUT` (default 900 s).
6. **Did it actually do anything?**: compare a working-tree fingerprint from before and after, and
   require a one-line receipt in `decisions.log`. Neither one → requeue (bounded by 3 attempts)
   rather than commit the offset and lose the session forever. A worker that dies (signal, timeout,
   missing python) is requeued by its own EXIT trap or, for SIGKILL, by the drain.
7. **Secrets guard + commit**: `ldn_commit_guard` in `_guard.sh`. The commit takes only paths that
   were clean before the worker ran and changed after (`git commit --only`); protected files
   (any case) are undone, not committed. A file YOU edit by hand while the worker runs (up to
   `LEYDEN_CAPTURE_TIMEOUT`) can land in the capture commit: it goes through the same secret scan,
   and the daily push would have committed it anyway, so only the commit label differs.

`leyden charge <transcript>` goes through `leyden-drain.sh --one`, which takes the same drain lock;
when a drain is already running it queues the transcript instead.

Activity is logged to `<KB>/.leyden/capture.log` (gitignored).

`LEYDEN_DRY_RUN=1` runs all of the above but writes no offset, log, queue or retry entry and commits nothing, for testing against a live transcript:
```
LEYDEN_DRY_RUN=1 LEYDEN_BG=1 OFFSETS_DIR=/tmp/off \
  LEYDEN_TRANSCRIPT=<path.jsonl> ./leyden-capture.sh
```

## Shared helpers

- **`_transcript.py`**: the ONE transcript parser. Modes: `prose`, `tools`, `emails`, `render`,
  `has-new` (exit 0 new conversation / 3 metadata only), `is-capture-worker`. Used by both `leyden-capture.sh` and `leyden-reconcile.sh`; they previously
  carried hand-copied inline parsers that drifted apart.

  Its job is separating *evidence of work* from *the KB talking about itself*. `open.md` names every
  open entity, so any session that displayed the dashboard, ran the lint, or grepped the KB used to
  register as touching every entity at once. `tools` mode therefore drops: KB-owned paths and
  content, results of enumeration-only tools (`ls`/`find`/`Glob`, since a path in an `ls` is not
  work while the same path from a `Write` is), and blocks naming 4+ distinct entities (a list
  being echoed).
  `render` mode keeps everything but tags KB traffic `[KB-INTERNAL]`, because the worker still needs
  to see prior KB writes to update them in place instead of duplicating.

- **`_guard.sh`**: exposes `SECRET_RE`, `ldn_commit_guard`, `ldn_git_lock`/`ldn_git_unlock`,
  `ldn_entity_pattern` (word-bounded ERE alternation of entity slugs plus configured aliases,
  shared by capture and reconcile so they cannot disagree about who counts as an entity: an
  unbounded pattern once matched a short slug inside random base64 and produced nothing but false
  hits), `ldn_canonicalise` and `ldn_mtime`. **Do not rename this file to anything matching
  `*_secret*`**: `.gitignore` has
  filename-shaped secret patterns, and they silently kept an earlier name out of every commit, so
  a fresh clone was missing the one file all the hooks source.

  On a secret hit the guard REDACTS in place via `leyden-redact.py` (see below) and commits the
  redacted entry: the knowledge lands, the value never does. The unstage-and-quarantine response
  survives only as the backstop for files the redactor cannot rewrite (binaries, non-UTF-8). An
  earlier guard ran a full reset/checkout/clean across the whole KB on any hit, destroying every
  uncommitted change.

  `ldn_canonicalise` maps a matched registry alias back to its folder slug. It is mandatory on
  anything `ldn_entity_pattern` matched, because grep emits whichever alternative hit: without it
  an entity referred to by its short name counts as a different entity from its own folder, which
  put every aliased entity permanently in the reconcile gap list and listed the same entity twice in the
  capture worker's hint list.

  `ldn_mtime` is the portable file mtime. `stat -f %m` is BSD/macOS only and `stat -c %Y` is GNU
  only; the callers here compute a lock's age from it, and on Linux the BSD-only form made every
  age come out as 0, so a stale lock was never reaped.

  `ldn_git_lock`/`ldn_git_unlock` serialise the capture commit against the push job's commit: both
  are backgrounded from the same SessionStart hook and used to race on `.git/index`, corrupting one
  job's staged files with the other's half-written state. The lock stores its owner's pid so a
  killed holder is reaped instead of blocking forever.

- **`leyden-redact.py`**: the secret detector/redactor (logic from gitleaks + detect-secrets).
  Rule table (keyed assignments incl. prefixed vars, bearer/JWT, connection-string passwords,
  signed-URL signatures (Azure SAS / S3 / GCS), Slack/GitHub/AWS token formats, PEM blocks) plus a Shannon-entropy fallback for
  unlabeled base64 strings. Redacts the SPAN only: `<REDACTED:<rule>:sha256/<8>>`, so a
  connection string keeps user/host and a signed URL stays diagnosable; the hash prefix identifies
  WHICH credential leaked (for rotation) without storing it. High-entropy HEX is report-only
  (git SHAs and image digests are indistinguishable from keys), URL path segments are exempt
  (chat/doc links are KB content), and a line containing `pragma: allowlist secret` is never
  touched: deliberate doc examples survive. Redactions are logged to `<KB>/.leyden/redactions.log`
  (gitignored). `--scan` mode audits without rewriting (works on stdin too, e.g. `git log -p |
  leyden-redact.py --scan -`). No filter makes a LIVE secret safe to push: if something real is
  found, rotate it.

- **`_offsets.sh`**: per-consumer commit offsets (Kafka-style consumer groups) in
  `<KB>/.leyden/offsets/<consumer>.tsv` (`<transcript_path>\t<byte_offset>`). Advancing or hand-editing ONE
  consumer's offset can no longer blind the others, and any consumer can be reset to replay the log
  from any point: the fix for an earlier mixed-session, manual-ledger-marking loss where a single
  shared ledger let one consumer's advance silently drop another's work. Assumes a single writer
  per consumer; the drain lock provides that. Commits are atomic (temp file + `mv`) so a crash
  mid-commit never corrupts the offset file.

## Retrieval: FTS5 index + frecency (`leyden-index.py`)

Logic borrowed from two proven open-source designs: zk (a disposable SQLite FTS5 index over
markdown; the files stay the only source of truth) and zoxide (frecency + aging).

- **`<KB>/.leyden/index.db`** (gitignored): FTS5 table over path/title/description/tags/symptoms/body,
  porter stemming, BM25 with per-column weights (`symptoms` 30 > `title` 10 > `description` 6 >
  `tags` 5 > body 0.4, body deliberately low so the large per-entity worklogs can't outrank the
  short playbook that is actually about the problem). Rebuilt in one pass (~milliseconds) by the
  push job, and lazily by the search hook whenever the indexed file set or any file's mtime/size
  differs from the signature stored in the index (so added, edited, deleted and renamed entries
  all show up on the next prompt). Directories named `scratch/` or `tmp/` are never
  indexed. If it is ever wrong, delete it; the hook falls back to the grep engine and rebuilds
  next prompt.
- **Relevance floor**: a hit is injected only if its score reaches `retrieval.min_score`
  (default 3.0) and `retrieval.min_score_ratio` (default 0.35) of the best hit's; the grep engine
  requires two of the query's words in a file. A prompt with fewer than two content words gets no
  search unless the word is a ticket id, an error code with digits, or an entity name.
  `leyden tap` skips the floor and the word count, and also takes 2-character ids with a digit
  (`k8`, `x1`).
- **Frecency**: the capture worker bumps `rank:` +1 and stamps `last_used:` on a playbook whose
  fix was actually *applied* in a session (per `capture-prompt.md`; retrieval alone never bumps).
  At query time: `final = BM25 relevance x (1 + log2(1 + rank x recency_bucket))`, buckets
  x4 (=7d) / x2 (=30d) / x1 (=90d) / x0.5 (older), relevance dominates, credibility lifts,
  and alias-only matches still never displace matches on the user's own words.
- **Aging** (`leyden-index.py --age`, run by the push job; `--dry-run` to preview), zoxide's
  algorithm: when total rank exceeds `LEYDEN_MAX_RANK` (default 500), scale every rank so the
  total returns to 90% of the cap; ranks falling below 1 lose their `rank:`/`last_used:` keys
  (the file stays, the boost dies). Old popularity fades unless re-earned.
- **Demand log**: every query appends one line to `<KB>/.leyden/metrics/search-hits.log` (gitignored):
  terms, engine, hit paths or `MISS`. Misses are knowledge that should exist and doesn't; reuse
  ranks tell you which playbooks earn promotion to the team's official docs. `LEYDEN_NO_INDEX=1`
  forces the grep engine for testing/bisecting.

## Health: the session-start line and `leyden doctor`

`tools/_health.sh` (`ldn_health`) is the one place that decides whether the pipeline is healthy. It
reads files and asks git two questions (no python, no lint run), so the SessionStart hook can call
it on every session. Each problem is one plain sentence with the fix:

- no git `user.name` / `user.email` (every background commit would fail)
- commits waiting for the remote for more than 48 h
- 5+ sessions waiting in the capture queue, or the last capture outcome was a failure
  (`commit FAILED`, `ABANDONED`, everything held back as secret)
- files held back in `.leyden/.secret-quarantine`
- lint failures, from the last daily `metrics/health.log` line
- any one-sentence `<KB>/.leyden/health/*.flag` a pipeline job writes and removes when the
  condition clears: `repo-busy` (a stuck git lock or a rebase in progress), `commit-failed`,
  `push-blocked`, `push-secret`, `capture-cli` (the `claude` CLI failed to run)

The hook joins up to four of them into one bold line; `leyden doctor` prints each as a WARN, next to
its install checks (FAIL on no git identity, no `claude` CLI, no `leyden.yaml`, hooks that do not
resolve; WARN on no remote, another knowledge base's hooks in the same settings file, a `/kb-sweep`
skill that is not Leyden's, no capture for 7 days).

## Health tools

Stdlib-only Python, run by `leyden-push-if-stale.sh` before each batched commit (best-effort, output
in `<KB>/.leyden/capture.log`), and runnable by hand any time:

- **`leyden-lint.py`**: conformance + health gate. FAIL: missing frontmatter/type, bad case
  status, dangling relative links. WARN: missing description/timestamp, dangling frontmatter
  relations, dashboard (`open.md`) out of sync with case statuses. STALE: content committed after
  its `timestamp:` (commits that only changed the `timestamp:` line itself do not count, so a
  restamp never re-flags the file), or an open/blocked case untouched for 14+ days. Exit 1 on FAIL
  (`--stale-fail` to also gate on stale).
- **`leyden-issues.py`**: regenerates `closed.md` and `issues.html`. Never hand-edit those.
  `--sync-open [--fix]` reconciles `open.md` against the case files. It matches links by TARGET
  (any link into the cases directory), not by link text: requiring the text to be literally
  `[issue]` made it blind to every hand-written row, so status cells were never corrected and
  terminal rows never dropped while the lint kept reporting the drift. A fat row routinely links
  several cases (its own plus the resolved siblings it grew out of), so the row is dropped only
  when EVERY case it links is terminal, and the status cell is corrected only when exactly one
  linked case is still open.
- **`leyden-graph.py`**: regenerates `graph.html` (gitignored): an interactive D3 force graph of the
  whole KB with a built-in page viewer.
- **`leyden-metrics.sh --daily`**: one self-throttled health line per day into `<KB>/.leyden/metrics/`.
  The lint count in the report comes from a fresh lint run, not from the last push cycle's log.
  "wrote" counts captures that actually committed (a requeued attempt is "retried", not "wrote").
  The report's COST section sums `<KB>/.leyden/cost.log` (one line per headless run:
  `<ISO time>`, `<capture|sweep>`, `model=`, `cost_usd=`, `in=`, `out=` ..., tab-separated) for the
  last 7 days and the report window.
- **`leyden-reconcile.sh [WINDOW_DAYS] | --weekly`**: the backstop for sessions the pipeline
  skipped or abandoned. Reports entities with real tool-level evidence in the window but no recent
  log entry. `--weekly` self-throttles and is called from the load hook. Tune the noise floor with
  `LEYDEN_MIN_EVIDENCE` (default 5 mentions). Transcript directories are found with Claude Code's
  own path encoding (every non-alphanumeric character becomes `-`), falling back to a scan for
  transcripts whose `cwd` is the workspace; with none yet (new install) it says so and exits 0.

## Pushing

`leyden-push-if-stale.sh` regenerates the dashboards, snapshots workspace config, commits, and
pushes, at most once per `LEYDEN_PUSH_INTERVAL` (default 72000s = 20h, so the first session of the
day does it). The whole job is behind that gate, not just the `git push`: it used to regenerate a
750 KB `graph.html` and commit on *every* session start.

Once past the gate it **waits** for an in-flight capture drain (up to `push.drain_wait_seconds`,
default 900) instead of skipping the cycle. It must not run concurrently with a drain (the drain
edits KB files over minutes without holding the git lock, and `pull --rebase --autostash` would
stash its half-written output), but both jobs are launched from the same SessionStart hook and the
drain claims its lock first, so an immediate skip meant the push lost the race on nearly every
session that had anything queued, and a "first session of the day" push really landed only every
few days. After
waiting it re-checks the stale gate, in case a parallel session pushed meanwhile.

The ahead count, `pull --rebase --autostash` and `push` run under the same git-op lock as the
capture commit, and the drain lock is checked again once that lock is held. A failed pull or push
logs git's own reason. The job refuses to push (and logs why) when `origin` is the Leyden tool
repo or the branch tracks some other remote: see `leyden init --remote`.

## Stale-case sweep (`leyden-sweep.py`, `/kb-sweep`)

Open cases pile up when nobody tells the KB they were fixed. The sweep closes that gap in two
phases, and a human always decides:

1. **Gather (read-only).** `leyden-sweep-gather.sh` runs `claude -p "/kb-sweep --gather"` from the
   project folder, weekly via launchd (`leyden sweep schedule`, Monday 09:00) or on demand
   (`leyden sweep gather`). The skill lists stale cases (`leyden sweep list --json`: who, a plain
   summary, search hints), fans out read-only subagents over Gmail, Slack and Linear, and writes
   `report-YYYY-MM-DDTHHMM.json/.md` into `<KB>/.leyden/sweep/`. It runs sandboxed: it never edits a
   case file and cannot send, draft or save anything (see `leyden-sweep-gather.sh` for the exact
   permissions).
2. **Review and apply (interactive).** `/kb-sweep` shows the report as one table and asks with
   checkboxes. Every question leads with who the case is for and a plain description of the problem;
   ticket ids only in parentheses. `leyden sweep apply <decisions.json>` writes only the ticked
   decisions: status, timestamp and a dated `## Sweep` note, then re-syncs `open.md` and regenerates
   the dashboards. Paths outside `<entities>/*/<cases>` and `internal/<cases>` are refused.

The SessionStart hook adds one line when a report waits for review or no sweep ran in 7 days.
`leyden init --write-hooks` links the skill into `<project>/.claude/skills/kb-sweep`.

## Reminders (`leyden remind`)

Dated follow-ups ("follow up on the staging cleanup tomorrow"). Delivery is a **Slack self-DM**; the tool
can't call Slack, so the assistant does both halves:

1. `slack_schedule_message` to the user's own Slack user id at the due time (text + links).
2. `leyden remind add <when> <text> [--ref <kb path>] --slack <scheduled message id>`.

`reminders.md` (KB root, checklist lines, hand-editable) is the record: `list [--all]`,
`done <id>`, `snooze <id> <when>`, `rm <id>`. Writes hold a lock (`.leyden/reminders.lock`), so
parallel sessions never lose a line; a `- [` line that no longer parses is reported on stderr by
`list` and `due`, never dropped silently. The load hook injects `due` (due now or within 24h) in its
header at SessionStart as a backstop for a missed DM; it prints nothing when none are due.
`reminders.md` is personal: lint, index and graph treat it as repo documentation, and it is never
shared. `<when>`: `2026-11-05 [HH:MM]`, `2026-11-05T14:30`, `today|tomorrow [HH:MM]`,
`mon..sun` or `monday..sunday [HH:MM]`, `+3d|+2h|+30m`; no time means 09:00 local, and the UTC
offset stored is the one in force on that day (DST safe).

When to set one: when the user asks, or unasked only for **important** dated follow-ups (a
temporary change to revert, a promised reply, an expiring credential). Say so in one line when
one is set. A scheduled DM can't be edited through the API; snooze/rm print a reminder to move
or cancel it in Slack "Drafts and sent".

## CLI

`bin/leyden` is the one entry point; run `leyden help` for the same list.

| Verb | Does |
|------|------|
| `init` | create or connect your KB (its own repo, `--kb PATH`): mode, vocabulary (preset or custom slots), identity, remote, hooks. Without a terminal it needs `--yes` and the answers as flags |
| `charge [transcript]` | capture: drain the session queue now, or capture one transcript |
| `tap <words...>` | recall: search the KB the way the prompt hook does, print the hits |
| `leak` | what is going stale: open cases untouched 14+ days, dashboard drift |
| `sweep [list\|apply\|status\|gather\|schedule\|unschedule\|schedule-label]` | weekly stale-case sweep: list stale cases, apply reviewed decisions, install the Monday launchd gather (see above); `schedule` warns when the `/kb-sweep` skill in the project is not Leyden's |
| `new <slot> <slug>` / `new <case\|context> <entity\|internal> <slug>` | scaffold an entry from the rendered templates (entity/playbook/project take a slug; a case or context also takes its entity, or `internal`) |
| `remind add\|list\|due\|done\|snooze\|rm` | dated reminders: Slack self-DM delivery, `reminders.md` record, SessionStart backstop (see Reminders) |
| `doctor` | install and pipeline health, PASS/WARN/FAIL per item with the fix; the hooks check runs each registered command's path the way a shell would (`--settings <file>` to point it at one) |
| `export --customer <slug> [--out DIR]` | handover bundle for one entity: profile, log, every case, every linked playbook, secrets removed from the copies, `HANDOVER.md` summary on top. Refuses to write inside the KB or the tool |
| `forget <slug\|path\|glob> [--apply]` | remove an entity or entries: plan first (files + what still mentions them), `--apply` deletes and commits only those paths, then prints the history rewrite (git filter-repo) and follow-ups without running them |
| `lint` / `index` / `dash` / `graph` / `metrics` / `reconcile` / `push` | run the matching health tool directly |
| `promote <file>` | stage a shareable case/entity file for the team's official docs or ticket tracker, with link rewriting and a secret scan |
| `team join <url-or-path> [--yes] [--clone-to DIR]` | find your clone of the team repo by remote URL under the workspace (depth 3) or take a path to a clone anywhere, or offer to clone it; records `team.shared_dir` (relative to the KB) in `leyden.local.yaml`, exports the team snapshot and writes the sharing rules (`templates/claude-md-team.md`) into the Leyden block of the workspace `CLAUDE.md`. The team repo needs no Leyden config |
| `team index-export` | refresh `<KB>/.leyden/team-snapshot/` from the team repo's merged default branch (what recall searches); also runs on `team join` and in the background at session start at most every 6 hours |
| `team status` / `team leave` | which team repo is connected and how fresh its snapshot is / disconnect it (drops the snapshot and the team section of the `CLAUDE.md` block) |
| `share` | list resolved cases with no `shared:` line yet (`tools/leyden-share.sh`). It shares nothing. To share one, ask Claude in the session ("push the acme export-timeout issue to the team repo"); `shared: no` on a case takes it off the list |

## Team mode (`tools/leyden-team.py`)

Design: the personal KB is the dump, the team repo is the curated record, flow is personal to team
only, always by pull request, merged by a person.

- **Config**: none in the team repo. Each member's connection is `team.shared_dir` in their own
  `leyden.local.yaml` (gitignored), stored relative to the KB.
- **Sharing is done by Claude**, in the session, only when the user asks. The rules live in
  `templates/claude-md-team.md`, which `leyden init` and `leyden team join` write into the
  workspace `CLAUDE.md` between `<!-- leyden:start -->` and `<!-- leyden:end -->`. Format, first
  that exists: the team repo's own template, existing records in the same customer folder, the
  most recent records elsewhere, a default (summary, symptom, environment, root cause with
  evidence marked confirmed or suspected, fix, ruled out, references). Claude removes
  credentials, works on a new branch off the default branch, shows the diff, asks the user once,
  pushes, opens a pull request and adds `shared: <url>` to the personal case. A person merges.
  There is no automatic share and no schedule.
- **Recall**: when a team repo is connected, the prompt hook also searches it: the snapshot of the
  MERGED default branch's markdown (only `.md`: config files such as `.env` or `config.yaml` can hold
  live secrets and are never copied into the KB folder) in `.leyden/team-snapshot/` when present (refreshed by `team join`,
  at session start at most every 6 hours, and by `team index-export`), else the clone. Own index `.leyden/team-index.db`, rebuilt lazily; hits
  labelled `team:<repo>/<path>`; each source's scores normalised to its own best hit before merging
  (team first at equal score: the team repo is the reviewed record; `team.prefer: false` in
  `leyden.yaml` puts personal first); the grep fallback covers the team repo too.
- **The team repo is never edited**: Claude goes by it over the personal KB when both cover
  something, and the only write is a new record on a branch when the user asks to share.
- **Team-specific templates live in the team repo**; Claude reads them when it shares.
