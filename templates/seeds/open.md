---
type: index
author: {{author}}
title: Open {{cases}}
description: Dashboard of every open {{case}}. Status cells are corrected from each {{case}} file on every push; edit the narrative here, edit the status in the file.
timestamp: {{now}}
---

# Open {{cases}}

Status is owned by each {{case}} file's `status:`; `tools/leyden-issues.py --sync-open` corrects
this table and drops resolved or discarded rows on every batched push. Resolved and discarded
{{cases}} appear in the generated `closed.md`.

| {{Case}} | {{Entity}} / area | Status | Blocked on |
|---|---|---|---|
