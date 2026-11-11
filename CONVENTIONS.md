# Conventions

Follow these so the bundle stays consistent and machine-parseable. The format is deliberately
simple: just markdown, just files, just YAML frontmatter.

Leyden has four renamable slots, set once by `leyden init` and named in `leyden.yaml`: **entity**
(who the work is for, top-level folder; `customer`/`customers` in the deployment preset),
**context** (one concrete setup under an entity; `deployment`/`deployments`), **case** (one thing
that went wrong or was asked; `issue`/`issues`), and **playbook** (a reusable, entity-agnostic fix;
`runbook`/`runbooks`). Two folders are fixed and never renamed: `internal/` (cases with no entity,
keyed on `area:`) and `projects/` (initiatives). The root also carries a fixed `index.md`,
`open.md`, generated `closed.md`/`issues.html`, and a root `log.md`; each entity folder carries its
own `log.md`.

Layout: `<entities>/<slug>/{index.md, log.md, <contexts>/, <cases>/}`, `<playbooks>/`,
`internal/<cases>/`, `projects/`, plus `_templates/` (rendered in your KB's own vocabulary by
`leyden init`). This document names the generic slot first and shows the deployment preset word in
parentheses as the running example; your KB may use different words throughout (`leyden new` and
`_config.py taxonomy` always tell you which).

Drafts and scratch files do not belong in the KB tree. If a `scratch/` or `tmp/`
directory does end up under it, the search index and the search hook skip it at any depth.

## Frontmatter

Every file starts with a YAML block. Only `type` is strictly required; fill the rest where they apply.

```yaml
---
type: <index | log | project | entity | context | case | playbook>   # REQUIRED
author: <engineer slug>               # who owns this file (leyden.yaml / leyden.local.yaml engineer.slug)
title: <human title>
description: <one-line summary used for retrieval>
tags: [<short>, <topical>, <tags>]
timestamp: <YYYY-MM-DDTHH:MM:SSZ>      # last meaningful update
---
```

`type:` is always a slot's singular name (`customer`, `deployment`, `issue`, `runbook` in the
deployment preset) or one of the three fixed types: `index`, `log`, `project`.

### Type-specific fields

| slot | extra fields |
|------|--------------|
| entity (`customer`) | `kinds: [production \| pilot]` (or your own kind list), `region`, `ticket_project` |
| context (`deployment`) | `<entity>: <slug>`, `kind`, `host`, `version`, `runtime`, `hardware`, `os` |
| case (`issue`) | `<entity>: <slug>` **or** `area` (see below), `ticket_id` (legacy `linear_id` accepted), `status: [open \| open-diagnosed \| in-progress \| blocked \| workaround \| review \| resolved \| discarded]`, `<context>: <slug>`, `<playbooks>: [<slug>]` |
| playbook (`runbook`) | `tags`, `affects` (list of ticket IDs seen), `applies_to` (versions / kinds), `symptoms` (3-5 phrases someone would SEARCH when hitting this: symptom words + exact error strings, not fix words; weighted highest by the search index), `rank` + `last_used` (frecency: maintained by the capture worker when the fix is actually applied, aged by `tools/leyden-index.py --age`; never hand-edit) |
| log | `<entity>: <slug>` (per-entity worklog only; the root `log.md` carries no entity) |

## Types: what each is for

- **playbook** (`runbook`): a reusable fix for a problem that recurs across entities. Entity-agnostic.
  Sections: Symptoms · Root Cause · Diagnosis · Resolution · Prevention. This is the single source;
  cases link to it instead of re-describing the fix.
- **entity** (`customer`): one profile per entity (`<entities>/<slug>/index.md`). Who they are, kinds,
  region, contacts, links to their contexts and cases.
- **context** (`deployment`): one concrete setup. An entity can have several (e.g. a production
  instance plus a staging one). Holds the concrete env facts (host, version, runtime, hardware, OS).
- **case** (`issue`): one incident/ticket/defect. Links to the context it hit and the playbook(s) that
  apply. **Every case gets a file, entity or not:**
  - entity-owned → `<entities>/<slug>/<cases>/<id>-<slug>.md`, keyed on `<entity>:`
  - no entity (product defect found internally, docs/platform bug, tooling problem) →
    `internal/<cases>/<slug>.md`, keyed on `area:` (`product/<component>`, `docs`, `platform`,
    `tooling`)
  A `projects/` file is for an *initiative* (a build, a doc sweep, a migration) and has no per-case
  `status:`, so it is not a substitute: a defect discovered inside a project still gets its own case
  file, cross-linked to the project. Cases without a file are invisible to `open.md`, `closed.md`,
  and the lint's staleness check.
- **log**: `<entities>/<slug>/log.md`, the running dated worklog for an entity. Append-only stream
  of short entries. This is the default home for daily work, including small unreported asks (no
  ticket). When a logged ask grows, promote it to its own case file and keep linking.

## Capture contract (what gets auto-persisted)

Capture is a three-stage pipeline (a session that is ending can't run heavy work itself):
SessionEnd (`tools/leyden-enqueue.sh`) enqueues the transcript path → the next SessionStart
(`tools/leyden-load.sh`) drains the queue in the background → the worker
(`tools/leyden-capture.sh`) dedups against the per-consumer offset store, pre-filters on conversation
text and non-KB tool payloads, then runs a headless Sonnet pass following `tools/capture-prompt.md`
and must leave a decision receipt in `.leyden/decisions.log` (in the KB) either way. In short:

- **WRITE** when: user was told to go check / come back; a finding, root cause, or decision landed on
  a case; a shareable artifact (chat/email/ticket/PR) was produced; an entity env fact changed; or
  the user said log/save/write-up.
- **DON'T** when: generic questions, pure lookups, no-decision chatter, or already captured in a
  ticket.
- Default target is the entity `log.md`; tracked tickets also get a case file; reusable fixes
  become playbooks. `open.md` (the dashboard) is kept current.

Entry format in `log.md`:
```
### YYYY-MM-DD - <ID or topic> <short title>  [finding|decision|artifact|env|next-step]
- <3-6 tight bullets: what was found/decided, next, blocked-on, refs>
```

## Dashboards

| File | Scope | Maintained by |
|------|-------|---------------|
| `open.md` | Currently-open cases + asks, with blocked-on detail. Injected at session start. | hand / capture hook |
| `closed.md` | The mirror of `open.md`: resolved + discarded cases in the same fat-row shape, plus the counts for all three buckets. Committed and grep-able. | generated, `leyden dash` |
| `issues.html` | Every case, every status, in one filterable table (bucket, status, entity/area, free text; sortable). Gitignored. | generated, `leyden dash` |

Never hand-edit `closed.md` or `issues.html`: fix the `status:` in the case file and regenerate
(`leyden dash`, or let the batched push hook do it). Lint warns when an open case is absent from
`open.md`, when a terminal-status case is still listed there, and when `closed.md` is missing a
closed case or still carries an open one.

### Status lifecycle

`open` (reported, not root-caused) → `open-diagnosed` (root-caused, fix not applied) →
`in-progress` (fix being worked) → `review` (fix in review/verification) → `resolved`.
Side states: `blocked` (waiting on someone external), `workaround` (mitigated, cause not fixed),
`discarded` (dropped on purpose: not-a-bug, superseded, won't-fix, or the ask went away, kept so the
decision is not re-litigated). `resolved` and `discarded` are the only terminal states, and both drop
off `open.md` and appear in `closed.md`.

## Slugs

- Entity folder: lowercase-kebab of the name (`example-corp`, `acme-industrial`).
- Case file: `<ticket-id>-<short-slug>.md` (`tck-102-cache-warmup-stall.md`); no ticket yet →
  just `<short-slug>.md` (`service-readiness-timeout.md`).
- Playbook file: `<problem-slug>.md` (`disk-quota-exhausted.md`).

## Linking

Use normal relative markdown links; they form the graph that makes this useful:

- Case → playbook: `[disk quota exhausted](../../../runbooks/disk-quota-exhausted.md)`
- Case → context: `[acme-prod-01](../deployments/acme-prod-01.md)`
- Entity → case: `[TCK-101](issues/tck-101-acme-export-timeout.md)`
- Always link a ticket by its URL in the body so you can jump back to the tracker (`url_base` in
  `leyden.yaml`'s `ticketing:` block).
- The templates in `_templates/` mark links you still have to fill as `TODO:` lines, not as
  markdown links. A link whose target still contains a `<placeholder>`, or a frontmatter value
  that does, is a lint WARN ("unfilled placeholder"), not a FAIL; links inside HTML comments are
  ignored.

## Secrets: never commit

Entity config, env maps, playbooks, account IDs, contact names, OAuth client IDs: **fine**
(identifiers, not secrets). Live credentials, client secrets, tokens, private keys, `.env`
contents: **never**. Reference where they live instead (e.g. "client secret in the secrets vault").

Enforcement layers, in order: the capture prompt tells the worker to reference-not-copy; every
commit runs `tools/leyden-redact.py`, which replaces a detected secret span with
`<REDACTED:<rule>:sha256/<8>>` and commits the entry anyway (knowledge is never sacrificed to the
filter); `.gitignore` blocks secret-shaped filenames. A deliberate fake example in a playbook is
kept by putting `pragma: allowlist secret` on the same line (in markdown:
`<!-- pragma: allowlist secret -->`). None of this makes a LIVE secret safe: if a real one is
caught (see `.leyden/redactions.log` in the KB), rotate the credential; the sha256/8 prefix in the marker
identifies which one it was.
