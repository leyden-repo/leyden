---
name: leyden-setup
description: Guided setup of Leyden (the Claude Code knowledge-base kit) for this workspace. Asks single or team, where the personal KB repo lives, which words to use, and the team repo URL, then runs `leyden setup` non-interactively and finishes with `leyden doctor`. Use when the user says "set up leyden", "install leyden", "/leyden-setup", or asks to connect Leyden to a team repo.
---

# Leyden setup

Goal: the user answers three or four short questions, confirms once, and ends with `leyden doctor`
all PASS. Never ask about ticket URLs, KB names or identity: `leyden setup` derives them.

## Step 0: find the tool and the workspace

The tool checkout is the folder two levels above this SKILL.md (it holds `bin/leyden`). Call it
`<tool>`; quote it in commands if it has spaces. The workspace is the folder that holds `<tool>`
(usually the folder Claude was started in). If Claude was started inside `<tool>` itself, that is
fine: `leyden setup` always uses the tool's parent as the workspace, never the current directory.

Run `<tool>/bin/leyden setup --personal local --dry-run` once, only to read the `you` line and
any problem. If it says git `user.name`/`user.email` are not set, ask the user for their name and
work email (the one exception to "no identity questions") and run
`git config --global user.name "<name>"` and `git config --global user.email <email>`.

Check whether `gh` is usable: `gh auth status` (exit 0 = yes). Only then offer "create one".

## Step 1: ask (AskUserQuestion, all in one call when possible)

1. "Solo or team?" options: `Solo` (your private KB) / `Team` (your private KB plus a shared team
   repo that receives pull requests). Both modes have the private KB.
2. "Where should your KB be backed up?" options:
   - `Create one for me` (only when gh works): runs `gh repo create <slug>-kb --private`
   - `I have an empty private repo`: the user picks Other and pastes its URL
   - `Nowhere, this laptop only`: say it is not backed up; a repo can be added later
3. "Which words fit your work?" (only when the dry run says the KB is `(new)`; an existing KB keeps
   its words) options, in this order, with NO recommendation (the user chooses):
   - `Deployment`: customer / deployment / issue / runbook -> `--vocabulary deployment`
   - `Application`: customer / use-case / blocker / playbook -> `--vocabulary application`
   - `Support`: account / environment / ticket / how-to -> `--vocabulary support`
   Say once that it names the folders: pick once, changing later means moving files by hand. (`product` and `generic` exist
   too, if the user asks.)
4. Team mode only: "Team repo URL?" (the user picks Other and pastes the URL or a local clone path).

A URL the user pastes goes to the command exactly as given.

## Step 2: confirm once

Run the same command with `--dry-run`, then say what will happen in one or two plain sentences,
not the raw output. For example: "I'll create your knowledge base in ~/work/ann-kb, back it up to
ann-kb.git, and turn on Leyden for this folder." In team mode add: "and bring the team repo into
~/work/team-kb." Ask one yes/no question: "Go ahead?" Stop on no. Finish the same way:
"All set, everything checks out. Restart Claude Code in ~/work to start using it." Show doctor
details only if something failed.

## Step 3: run it

```bash
<tool>/bin/leyden setup --personal <url|new|local> [--vocabulary <name>] [--team <url>]
```

It is non-interactive and idempotent: re-running the same command after a fix is always safe.
It ends with `leyden doctor`. If any line is FAIL, show that line; it names the fix. Apply it (or tell the user what to do when it needs them, e.g. logging in to `claude` or
`gh`), then re-run the same command. Do not edit `.claude/settings.json` by hand.

## Step 4: finish

Tell the user, in two or three lines: where the KB is, that it fills itself from sessions, and
that they must **restart Claude Code in the workspace folder** (not inside `leyden/`) so the hooks
load. Optional next step: `<tool>/bin/leyden sweep schedule` for the Monday sweep.

## Discoverability

Before setup the user just says "set up leyden" in the workspace: Claude finds this file by reading
`leyden/` (README and CLAUDE.md point here; verified with real Claude). After setup, `leyden setup`
links this skill into `<workspace>/.claude/skills/leyden-setup`, so `/leyden-setup` works for re-runs
and team joins.
