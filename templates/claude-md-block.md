## Knowledge base (Leyden)

Check `{{kb}}/` before starting {{entity}} work: `open.md` for prior context, `{{playbooks}}/` for a
known fix, `{{entities}}/<slug>/` for that {{entity}}'s history. Leyden captures sessions into it
automatically; edit it directly when asked.

The user talks to you, not to the CLI. When they ask for one of these, run `{{tool}}/bin/leyden`
yourself and report the result in plain words:
- search the KB ("what do we know about X"): `tap <words>`
- health ("is leyden ok", "is capture working"): `doctor`
- stale work ("what is going stale"): `leak`
- reminders ("remind me tomorrow at 10 to ..."): `remind add <when> <text>`, `remind list`,
  `remind done <id>`, `remind snooze <id> <when>`, `remind rm <id>`; <when> is today, tomorrow, mon..sun, YYYY-MM-DD,
  +3d, +2h, +30m, optional time (10, 10:30, 3pm)
- not yet shared with the team: `share`
- join or leave a team repo: `team join <url> --yes`, `team leave`; connection: `team status`
- capture or push now: `charge`, `push`
- update Leyden ("update leyden"): `git -C {{tool}} pull`, then `doctor`
- cost ("what did capture cost this week"): `metrics`
- weekly stale-issue sweep: `sweep schedule` (on), `sweep unschedule` (off), review with /kb-sweep
- handover bundle for one {{entity}}: `export --customer <slug>`
{{team_block}}
