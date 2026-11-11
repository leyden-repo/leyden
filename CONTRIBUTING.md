# Contributing to Leyden

Thanks for helping. Bug reports, fixes and new redaction rules are all welcome.

## Before you start

- For a bug, open an issue with the output of `leyden doctor`. Remove anything private from it first.
- For a bigger change (a new command, a new hook, a change to the KB layout), open an issue to
  discuss it before you write the code.
- For a security problem or a secret the redactor misses, follow [SECURITY.md](SECURITY.md)
  instead of opening a public issue.

## Development setup

Leyden is plain bash and Python 3.9+ with no dependencies to install.

```bash
git clone <your-fork-url> leyden
cd leyden
```

Test against a throwaway knowledge base, never your real one. Setting `HOME` to an empty folder
keeps Leyden away from your real Claude Code settings:

```bash
export HOME="$(mktemp -d)"
./bin/leyden setup --personal local --workspace "$HOME/work"
```

## Checks to run before a pull request

```bash
for f in bin/leyden tools/*.sh; do bash -n "$f" || echo "FAIL $f"; done
python3 -m py_compile tools/*.py
python3 tools/leyden-redact.py --selftest
```

All three must pass. The same checks run in CI.

## Rules for content

- **No real data, anywhere.** Test fixtures, examples and docs use invented names (`acme`,
  `example.com`, `123456789012`) and invented secrets. Never paste a value from a real system,
  even an expired one.
- A new redaction rule needs a positive selftest case (a made-up secret it must catch) and, where
  a false positive is likely, a negative case (normal text it must leave alone).
- Keep the hooks fast. `UserPromptSubmit` runs before every prompt.

## Pull requests

- One change per pull request, with a short description of what changed and why.
- Update `CHANGELOG.md` under "Unreleased".
- By contributing you agree your work is released under the [MIT License](LICENSE).
