---
type: {{case}}
author: {{author}}
title: <{{case}} title>
description: <one-line summary>
{{entity}}: <{{entity}}-slug>        # {{entity}} {{cases}} only; internal {{cases}} use `area:` instead
area: <product/<component> | docs | platform | tooling>   # internal {{cases}} only
ticket_id: <TICKET-XXX>                # leave empty if there is no ticket
status: open    # open | open-diagnosed | in-progress | blocked | workaround | review | resolved | discarded
{{context}}: <{{context}}-slug>
{{playbooks}}: []    # [<{{playbook}}-slug>, ...] once a {{playbook}} exists
timestamp: <YYYY-MM-DDTHH:MM:SSZ>
---

# <TICKET-XXX>: <short title>

**Ticket:** {{ticket_url_base}}<TICKET-XXX>
**{{Context}}:** TODO: link the {{context}} (`../{{contexts}}/<slug>.md`)
**{{Playbook}}:** TODO: link the {{playbook}} if one applies (`../../../{{playbooks}}/<slug>.md`), else remove this line

## What happened

<symptoms, error text, what was asked>

## Root cause

<root cause; link the {{playbook}} if it is a known recurring one>

## Fix

<the steps that fixed it, copy-pasteable; leave empty until it is fixed>

## Status / next steps

<current status, who is blocking, what is next>

## Related

- TODO: related {{cases}}, one link each (`<other-{{case}}>.md`)
