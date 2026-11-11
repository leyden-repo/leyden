# Leyden setup: individual mode

Individual mode is one person, one private knowledge base (KB). Nothing is shared with anyone. You
can switch to team mode later without losing anything.

## What you need

- macOS (Linux and Windows are not supported yet)
- `git` with your name and email set (`git config --global user.name` / `user.email`). Without them
  Leyden cannot commit your notes, and `leyden doctor` says so.
- `python3` 3.9 or newer (the macOS system one is fine). No pip installs.
- Claude Code, with `claude` on your PATH and logged in. Capture and the weekly sweep run it in the
  background and use tokens on your account.
- Optional: an empty **private** git repo for your KB, owned by your organisation (GitHub, GitLab).
  Without one the KB stays on your laptop only.
- Optional, for the weekly sweep: Claude Code connectors for Gmail, Slack and Linear. Without them
  the sweep still lists stale cases, it just finds less evidence.

## Setup (about 2 minutes)

1. Clone Leyden into the folder where you start Claude Code (your workspace), next to your other
   repos:
   ```bash
   cd ~/work
   git clone <url-of-this-repo> leyden
   ```
2. Start Claude Code in the workspace and say: "set up leyden".
   Claude asks:
   - just you, or a team (pick "just me" here; team mode is [setup-team.md](setup-team.md))
   - where to back up your KB: an existing EMPTY private repo (paste its URL), "create one"
     (runs `gh repo create <you>-kb --private`; offered when the `gh` CLI is installed and logged
     in), or local only, no remote (add one later with `leyden init --remote <url>`)
   - which words fit your work (new KB only; they become folder names and cannot be changed
     later): `deployment` (customer / deployment / issue / runbook), `application`
     (customer / use-case / blocker / playbook) or `support` (account / environment / ticket /
     how-to); `product` and `generic` also exist

   It shows the plan once (KB folder, remote, hooks file), runs the setup and shows the
   `leyden doctor` result: no FAIL lines (a WARN is advice, for example "no git remote" with `local`). Your name, email and KB name come from
   `git config`; nothing else is asked.
3. Restart Claude Code in the workspace so the hooks load, then ask "what is open?". The
   open-issues list goes to Claude, not to your screen; a Leyden line shows in the terminal only
   when something needs you (health problem, reminder due, sweep to review).
4. Optional: turn on the weekly sweep (Monday 09:00, read-only):
   ```bash
   ./leyden/bin/leyden sweep schedule
   ```

Without Claude, step 2 is one command (non-interactive; re-running it changes nothing and says so):

```bash
./leyden/bin/leyden setup --personal <private-repo-url | new | local> [--vocabulary <name>]
```

It creates `<workspace>/<you>-kb`, registers the hooks in `<workspace>/.claude/settings.json`,
adds the Leyden block to `<workspace>/CLAUDE.md`, links the `kb-sweep` and `leyden-setup` skills,
and runs `leyden doctor` (non-zero exit on any FAIL). `--dry-run` shows the plan without changing
anything. Giving it a repo URL that already holds your KB (a second laptop) clones it.

## What you get

| When | What happens | You do |
|---|---|---|
| Session start | Claude silently receives a short list of your open cases (ask "what is open?" to see it). A Leyden line shows on your screen only when something needs you: a health problem (capture failing, a push stuck for 48 h, no git identity, files failing lint, secrets held back), a reminder due, or a sweep report waiting for review | fix what the line names |
| Every prompt | Your KB is searched; up to 5 relevant entries are added before Claude answers. Chatter ("ok thanks") adds nothing | nothing |
| Session end | The transcript is queued; the next session start captures it in the background | nothing |
| First session start 20 h after the last push | The KB is committed and pushed to your private remote (once a day while you use Claude) | nothing (`leyden push` pushes now) |
| Monday 09:00, after `leyden sweep schedule` | Every case untouched for 14+ days is checked against Gmail, Slack and Linear (read-only) and a report is written. Each case is shown with who it is for and a plain description of the problem, plus the evidence | run `/kb-sweep` when the session start says a report is ready, tick which ones to close (about 5 minutes) |
| Whenever you want | `leyden doctor` (is anything broken), `leyden leak` (what is going stale), `leyden graph` (the dashboard) | |

Capture runs only when a session is about customer work: it names a known customer (folder or
alias), a ticket id with a prefix listed under `ticketing.prefixes` in `leyden.yaml`, an email
address at a customer's domain, or you typed the word "customer" (a new customer; capture then
creates the folder when it finds a real name). Other sessions cost nothing. Capture writes only into
your KB folder, and secrets (keys, passwords, tokens) are removed before anything is committed.

## Where things live

```
~/work/
  leyden/        # the tool. Pull-only: `git -C leyden pull` to update. Never put data here.
                 #   leyden/.kb-path (not committed) records where your KB is
  <you>-kb/      # your KB (its own git repo, your private remote)
    customers/<customer>/{index.md, log.md, deployments/, issues/}
    runbooks/    # reusable fixes, not tied to one customer
    internal/    # issues with no customer (product bugs, tooling)
    projects/    # longer initiatives
    open.md      # what is open right now
    graph.html   # the dashboard (generated, not committed)
    .leyden/     # machine-local state: logs, capture queue, search index, sweep reports, cost.log
  .claude/       # the hooks Leyden registered (settings.json) and the /kb-sweep skill
```

You can read and edit every file by hand. It is plain markdown.

## Everyday commands

| Command | What it does |
|---|---|
| `leyden doctor` | Health check with the fix for every problem |
| `leyden tap <words>` | Search the KB the way the prompt hook does |
| `leyden leak` | Open cases untouched for 14 days, dashboard drift |
| `leyden graph` | Rebuild the dashboard (`<you>-kb/graph.html`) |
| `/kb-sweep` | Review the weekly sweep report and close what is finished |
| `leyden export --customer <slug>` | Handover bundle for one customer (secrets removed) |
| `leyden forget <slug or path>` | Remove something from the KB (plan first, `--apply` to do it) |

`leyden help` lists the rest.

## Where your KB should live

Your KB holds customer names, environments and incident history. Keep its remote in a **private
repo owned by your organisation**, not a personal account, so the record survives a laptop or a
person leaving. Store what broke, why and how it was fixed; never store passwords, keys, tokens or
customer files. Write where a secret lives, not the secret.

## Updating and removing

- Update: `git -C leyden pull`. Your KB is untouched.
- Stop the weekly sweep: `leyden sweep unschedule`.
- Remove: delete the three Leyden hooks from `.claude/settings.json`, run
  `leyden sweep unschedule`, then delete `leyden/`. Your `<you>-kb/` stays; it is just markdown.
