
### Sharing to the team repo (`{{team_dir}}`)

The team repo is the curated, shared record. Flow is one way: personal KB to team repo, never back.
Share only when the user asks ("push the <{{entity}}> <{{case}}> to the team repo").

It is also the source of truth. When it and the personal KB both cover something (an {{entity}}'s
environment, versions, config, a known {{case}}), go by the team repo, and correct the personal note
to match. Never edit, reformat or "clean up" existing team repo files: some may be verbatim
copies of external files and must stay byte-for-byte as they are. The only change Leyden makes there is the new record in step 5, on a branch, when asked.

What goes: a resolved {{case}} another engineer could hit again, with its root cause; a change to the
{{entity}}'s environment record (versions, config). What stays personal: open or in-progress
{{cases}}, worklogs, raw notes, {{playbooks}}.

1. Find the format, in this order, and follow the first one that exists:
   a. a template the team repo provides (`_templates/`, `templates/`, or one its CLAUDE.md or README
      points to);
   b. existing {{cases}} in the same {{entity}} folder of the team repo (file naming, headings,
      frontmatter, any index table in `README.md`);
   c. the most recent {{cases}} anywhere else in the team repo;
   d. the default below.
2. Write the record from the personal KB {{case}}. Default shape when nothing exists:
   a 3 to 5 bullet summary (symptom, impact, cause, fix, status); symptom with the verbatim error;
   environment and versions; root cause in one paragraph citing evidence (source file:line, log line,
   ticket) and marked `confirmed` or `suspected`; numbered fix; what was tried and ruled out, one
   line each; references.
3. Remove credentials and anything about another {{entity}}. Internal links (chat threads, tickets)
   may stay.
4. If the folder has an index table (for example `issues/README.md`), add one row in its existing
   format. Never rewrite hand-written content.
5. Work on a new branch off the team repo's default branch, show the diff, ask the user ONCE before
   pushing, then push the branch and open a pull request. Never push to the default branch.
6. Add a line to the personal {{case}}: `shared: <pull request url>`. When no pull request can be opened
   (no `gh`, or a host it does not support), push the branch, give the user the branch name to open
   the pull request by hand, and write `shared: branch <name>`.
