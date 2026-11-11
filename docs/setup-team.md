# Leyden setup: team mode

Team mode is individual mode plus one shared team repo. Every engineer keeps a private KB (the raw
notes, drafts, everything). The good parts go to the team repo **by pull request only**. A person
merges each PR: the merge is the curation step. Nothing merges automatically.

```
 your sessions ──► your private KB ──(PR, when you ask)──► team repo ◄──(PR)── teammate's KB
                     (everything)                        (curated)
                         ▲                                   │
                         └──── recall reads both ◄───────────┘
```

Knowledge flows one way: personal to team. Nothing is written back into your private KB from the
team repo; the team repo is read for recall only.

There are two roles. The **team owner** picks the team repo once. **Members** connect to it.
Both need the individual setup first (`setup-individual.md`).

---

## What the team repo looks like

Any git repo your team already uses works. The team repo needs no Leyden config and Leyden never
writes to it on its own. A typical layout is one folder per customer:

```
<Customer>/
  README.md         deployment record, written by people
  contacts.md       who to talk to, written by people
  issues/
    README.md       issues index (a table, if your team keeps one)
    20250101-nightly-export-timeout.md   one record per issue
_templates/         optional: the team's own record templates (issue.md, contacts.md, ...)
```

If your team has its own record format (for example an incident template), keep it in the
team repo, for example under `_templates/`, so every member's Claude reads the same one.

## Team owner: prepare the team repo (about 5 minutes)

1. Pick the repo: an existing one (for example your team's customer-records repo) or a new empty one.
2. Protect the default branch: require a pull request and one approving review. Do **not** enable
   auto-merge.
3. Optional: add your record templates under `_templates/` (or point to them from the repo's README
   or CLAUDE.md).

Send members the repo URL.

## Member: join a team (about 5 minutes)

```bash
leyden/bin/leyden team join <team-repo-url>
```

`team join` looks for a clone of that URL in your workspace (up to three folders deep) and offers
to clone it if there is none (`--yes` clones without asking, `--clone-to DIR` picks where). You can
also pass the path of a clone anywhere on disk: `leyden team join ../team-kb`. It records the clone
in your `leyden.local.yaml` (never committed; stored relative to the KB, so moving the workspace
keeps it working), takes a snapshot of the team repo for recall, and writes the sharing rules into
the Leyden block of your workspace `CLAUDE.md` (between `<!-- leyden:start -->` and
`<!-- leyden:end -->`). `leyden init` writes the same block.

## Sharing a case

Sharing is done by Claude, in your session, only when you ask:

> push the acme export-timeout issue to the team repo

Claude then:

1. Picks the format, using the first of these that exists: a template the team repo provides
   (`_templates/`, `templates/`, or one its README or CLAUDE.md points to); existing records in the
   same customer folder; the most recent records elsewhere in the team repo; a default (a short
   summary, the symptom with the verbatim error, environment and versions, the root cause with its
   evidence marked `confirmed` or `suspected`, the fix, what was ruled out, references).
2. Writes the record from your case, removes credentials and anything about another customer, and
   adds one row to the folder's index table if there is one. Internal links (chat threads,
   tickets) may stay.
3. Creates a new branch off the team repo's default branch, shows you the diff and asks you once.
4. Pushes the branch and opens a pull request (with `gh` on GitHub; on other hosts it gives you the
   link to open it).
5. Adds `shared: <pull request url>` to your case.

A teammate reviews and merges. Nothing reaches the default branch without a person.

What goes: a resolved case another engineer could hit again, with its root cause, and changes to
a customer's environment record. What stays private: open or in-progress cases, worklogs, raw
notes, runbooks (copy a runbook into your docs repo by hand when it is ready to publish).

`leyden share` lists resolved cases that have no `shared:` line yet. It shares nothing. To take a
case off that list without sharing it, add `shared: no` to the case file.

## Recall from the team repo

Recall searches your KB first, then a snapshot of the team repo's merged default branch, so you see
teammates' merged fixes. Team results are labelled `team:<repo>`. The snapshot is refreshed by
`leyden team join`, at session start at most every 6 hours, and on demand with
`leyden team index-export`.

## Useful commands

| Command | What it does |
|---|---|
| `leyden team status` | which team repo you are connected to, and how fresh its snapshot is |
| `leyden share` | list resolved cases not yet shared |
| `leyden team index-export` | refresh the snapshot of the merged team repo that recall searches |

## Leaving team mode

```bash
leyden/bin/leyden team leave
```

This removes the connection from `leyden.local.yaml`, deletes the local team snapshot and removes the
sharing rules from the Leyden block of your workspace `CLAUDE.md`. Recall stops reading the team
repo; your private KB, your clone and everything already merged in the team repo stay as they are.
An older team repo may still hold a `leyden-team.yaml`, `ci/` or a share-gate workflow from an
earlier Leyden version; Leyden no longer reads them and the owner can delete them.
