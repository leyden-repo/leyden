# Security policy

Leyden handles text that can contain secrets, so security reports are taken seriously.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub: open the **Security** tab of this repository and click
**Report a vulnerability**. Include:

- what you found and how to reproduce it
- the Leyden commit you tested (`git -C leyden rev-parse --short HEAD`)
- your macOS, bash and Python versions

You should get a first reply within 7 days.

## Redaction misses

`tools/leyden-redact.py` removes secrets before anything is written to a knowledge base. If it
misses a secret format, report it the same way, privately.

**Never send a real secret.** Make up a value with the same shape (same prefix, length and
character set) and send that. The fix will be a new rule plus a selftest case built from your
made-up value.

## Scope

In scope: the redaction rules, the hooks, the capture worker sandbox, anything that could write
outside the knowledge base folder, and anything that could push a knowledge base somewhere other
than the remote you configured.

Out of scope: secrets you typed into a knowledge base by hand, and the security of the git host or
Claude Code itself.

## Supported versions

Only the latest commit on `main` gets fixes.
