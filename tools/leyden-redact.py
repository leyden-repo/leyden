#!/usr/bin/env python3
"""
leyden secret redactor: detect secrets in KB files and redact them IN PLACE, keeping the
knowledge. Replaces only the secret span with `<REDACTED:<rule>:sha256/<8 hex>>`; the hash
prefix lets you later confirm WHICH credential leaked (hash the suspect value, compare)
without the value ever being stored. Logic borrowed from gitleaks (provider rule table) and
detect-secrets (Shannon-entropy fallback, `pragma: allowlist secret` inline escape).

Design rules:
- Redact the SPAN, never the line or the file. A connection string keeps user/host readable;
  a SAS URL keeps everything but the sig= value. The entry stays useful (never nullified).
- A line containing `pragma: allowlist secret` is never touched: playbook-style entries may
  show deliberate fake examples, and the filter must never eat the fix documentation.
- Precision over recall for anything DESTRUCTIVE: hex-entropy candidates are reported but
  not auto-redacted (git SHAs and image digests are random hex, indistinguishable from
  keys). Base64-entropy candidates ARE redacted (length-normalised threshold, see B64_RATIO).
- A keyed value that reads as a NAME (`existingSecret: app-postgres-credentials`), a reference
  (`$VAR`, `<placeholder>`, `os.environ/X`) or a path is left alone; a PEM header mentioned in
  prose is left alone (only a header followed by a base64 body is a key).
- Idempotent: `<REDACTED:...>` markers are never re-matched.
- Every rule change is checked by `--selftest` (positive AND negative cases, python 3.9+).

Usage:
    leyden-redact.py --scan  FILE...   # report findings, exit 1 if any (0 = clean)
    leyden-redact.py --redact FILE...  # rewrite files in place, exit 1 if redacted
    leyden-redact.py --scan -          # scan stdin (e.g. a staged diff or `git log -p`)
    leyden-redact.py --selftest        # run the built-in must-redact / must-survive cases
Findings go to stdout as: <rule>\t<file>:<line>\t<REDACTED:<rule>:sha256/<8>>
No filter makes a LIVE secret safe: if something real is found, rotate the credential.

stdlib only.
"""

import base64
import hashlib
import math
import re
import sys
from pathlib import Path

PRAGMA = "pragma: allowlist secret"

# ── rule table (gitleaks-derived; the FIRST non-None capture group is the secret span) ─────────
# Provider-shaped rules run before the generic keyed-assignment rule so a finding carries the most
# specific rule name; anything already turned into a <REDACTED:...> marker is never re-matched.
KEYWORDS = (r"secret|password|passwd|pwd|token|api[_-]?key|client[_-]?secret|cookie[_-]?secret"
            r"|crypto[_-]?key|access[_-]?key|account[_-]?key|private[_-]?key")
# a keyed VALUE: double-quoted / single-quoted (any run without the quote: symbols and spaces are
# legal password characters, the old [A-Za-z0-9/_+.=-] class stopped at the first `!` or `&`), or
# unquoted (any run of non-space, non-quote characters). Lengths are enforced in _value_ok().
_VALUE = r"""(?:"([^"\n]+)"|'([^'\n]+)'|([^\s"'`]+))"""
RULES = [
    ("pem-inline", re.compile(  # JSON/env-escaped one-line PEM: "-----BEGIN ... KEY-----\nMIIE..."
        r"(-----BEGIN [A-Z ]*PRIVATE KEY-----(?:\\[nr]|\s)*[A-Za-z0-9+/=]{40,}"
        r"(?:(?:\\[nr]|\s)*[A-Za-z0-9+/=]+)*(?:(?:\\[nr]|\s)*-----END [A-Z ]*PRIVATE KEY-----)?)")),
    ("openai-key", re.compile(  # OpenAI-style keys (also used by LLM proxies): sk-..., sk-proj-..., sk-ant-...
        r"\b(sk-(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{20,})")),
    ("gitlab-token", re.compile(r"\b((?:glpat|gldt|glptt|glrt|glsoat)-[A-Za-z0-9_-]{20,})")),
    ("npm-token", re.compile(r"\b(npm_[A-Za-z0-9]{36})\b")),
    ("google-api-key", re.compile(r"\b(AIza[0-9A-Za-z_-]{35})")),
    ("slack-token", re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})")),
    ("slack-webhook", re.compile(r"hooks\.slack\.com/(?:services|workflows|triggers)/[A-Z0-9]+/[A-Z0-9]+/([A-Za-z0-9]{16,})")),
    ("google-oauth-token", re.compile(r"(?<![A-Za-z0-9])(ya29\.[0-9A-Za-z_-]{20,})")),
    ("sendgrid-key", re.compile(r"(?<![A-Za-z0-9])(SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,})")),
    ("refresh-token", re.compile(r"(?<![A-Za-z0-9.])(v1\.[A-Za-z0-9_-]{40,})")),
    ("github-token", re.compile(
        r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("aws-key-id", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    # Stripe secret / restricted keys: low-entropy test keys slipped past the entropy fallback.
    ("stripe-key", re.compile(r"(?<![A-Za-z0-9])((?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,})")),
    # Azure AD (Entra) client secret: 3 chars, a digit, `Q~`, then 31-34 chars (gitleaks shape).
    # `~` and `.` are legal inside it: the old keyed rule stopped at the `~` and the entropy
    # fallback then redacted only the part after the last `.`, leaving the head in clear.
    ("azure-client-secret", re.compile(
        r"(?<![A-Za-z0-9_~.-])([A-Za-z0-9_~.]{3}[0-9]Q~[A-Za-z0-9_~.-]{31,34})(?![A-Za-z0-9_~.-])")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._+/=~-]{20,})")),
    ("basic-auth", re.compile(r"(?i)\bauthorization:\s*basic\s+([A-Za-z0-9+/]{8,}={0,2})")),
    ("jwt", re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,})")),
    # connection string: redact ONLY the password; user/host/db stay readable. The user may be
    # empty (redis://:pw@host is the normal Redis shape).
    ("connection-string", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]*:([^@\s/]{6,})@")),
    # Signed URLs (Azure SAS sig=, AWS X-Amz-Signature=, GCS X-Goog-Signature=): redact ONLY the
    # signature value; the URL stays diagnosable
    ("sas-token", re.compile(r"(?i)[?&](?:sig|sig_|x-amz-signature|x-goog-signature)=([A-Za-z0-9%/+=]{16,})")),
    # CLI secrets: `<cli> login -p <pw>`, `--password <pw>`, `--account-key=<k>`. `-p` is also mkdir's,
    # ssh's and docker's flag, so every value goes through _value_ok() (paths, ports, kebab names
    # and $VARS survive) and `-p` additionally needs a secret-shaped value (_strong()).
    ("cli-secret-flag", re.compile(
        r"(?:^\+?|\s)(-p|--password|--passwd|--account-key|--client-secret|--secret|--token"
        r"|--api-key|--sas-token|--secret-key)(?:\s+|=)" + _VALUE)),
    ("cli-secret-flag", re.compile(r"\bmysql(?:dump|admin)?\b[^\n|]*?\s(-p)([^\s\"'`]{4,})")),
    # | password | <value> |  (the usual KB table shape)
    ("table-secret", re.compile(
        r"(?i)\|\s*[\w .-]*(?:" + KEYWORDS + r")\s*\|\s*`?([^|\s`][^|`]*?)`?\s*\|")),
    # prefix-tolerant: MY_SERVICE_API_KEY=... must match, not just api_key=... Only the
    # VALUE is captured/redacted, so the variable name stays readable in the entry
    ("keyed-assignment", re.compile(
        r"(?i)[A-Za-z0-9_.-]*(?:" + KEYWORDS + r")[\"']?\s*[:=]\s*" + _VALUE)),
    # The three rules below run AFTER keyed-assignment, so they only see what it left.
    # Product / activation keys: dash-separated groups of upper-case letters and digits, 4+ groups
    # (`AB-7F3A-99QX-K2M8-PL4Z`). Validated in _key_ok(): at least two groups mix letters and digits,
    # so ticket ids (ABC-123), SKUs and upper-case UUIDs (8-4-4-4-12) are left alone.
    ("license-key", re.compile(r"(?<![A-Za-z0-9-])([A-Z0-9]{2,6}(?:-[A-Z0-9]{4,6}){3,})(?![A-Za-z0-9-])")),
    # Prose: "the admin password is Pa55w0rd!Xq9", "pwd was hunter22". Validated in _value_ok with
    # a lower length floor than the keyed rule, but only for secret-SHAPED values (letters + digits).
    ("prose-password", re.compile(
        r"(?i)\b(?:password|passwd|pwd|passcode|passphrase)(?:\s+(?:for|of|on|to)\s+[\w .-]{1,40}?)?"
        r"\s+(?:is|was|->)\s+" + _VALUE)),
    # Short keyed password (`pwd = hunter22`): the generic keyed rule needs 10+ chars unquoted.
    # `(?<=_)` also takes env-style prefixes (DB_PASS=, MYSQL_PWD=), which \b alone missed.
    ("short-password", re.compile(
        r"(?i)(?:\b|(?<=_))(?:password|passwd|pwd|pass|secret)[\"']?\s*[:=]\s*" + _VALUE)),
]
# k8s/Helm object names, env var names, paths: `existingSecret: app-postgres-credentials`,
# `secret: aws_secret_access_key`. Lowercase, separator-delimited segments that are each plain
# words or plain numbers; short alnum segments (k8s, v1, gp3) are allowed. A random lowercase
# password (`kq8z3m2x9vt1`) has a long mixed segment and does NOT match.
_NAME_LIKE = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
# references / placeholders, not values: $VAR, <your-pw>, {{ .Values.x }}, [json-patch], ****,
# os.environ/NAME (proxy config env refs), a function call, a masked `Xq4M******b7Pw`
_PLACEHOLDER = re.compile(r"^(?:[$<{%*\[]|\.\.\.|x{4,}|changeme|example|redacted|os\.environ\b|env:"
                          r"|[A-Za-z_][\w.]*\()|\*\*\*", re.I)


def _name_like(v: str) -> bool:
    if not _NAME_LIKE.match(v):
        return False
    return all(seg.isalpha() or seg.isdigit() or len(seg) <= 4
               for seg in re.split(r"[-_./]+", v) if seg)


def _strong(v: str) -> bool:
    """Secret-shaped: at least two of upper / lower / digit / symbol, and not trivially repetitive."""
    classes = sum(bool(re.search(rx, v)) for rx in (r"[A-Z]", r"[a-z]", r"[0-9]", r"[^A-Za-z0-9]"))
    return classes >= 2 and shannon(v) >= 2.5


def _key_ok(val: str) -> bool:
    groups = val.split("-")
    mixed = sum(1 for g in groups if re.search(r"[A-Z]", g) and re.search(r"[0-9]", g))
    return mixed >= 2 and not UUID.match(val)


_PROSE_WORDS = re.compile(r"(?i)^(?:set|stored|saved|kept|in|the|a|an|not|wrong|correct|required|"
                          r"missing|empty|same|different|changed|reset|rotated|expired|valid|invalid|"
                          r"there|here|hidden|unknown|redacted)$")


def _value_ok(rule: str, flag: str, val: str, quoted: bool) -> bool:
    """Is a keyed / flag VALUE worth redacting? Precision filter for the generic rules."""
    if "REDACTED" in val or _PLACEHOLDER.search(val):
        return False
    if rule in ("prose-password", "short-password", "table-secret"):
        # secret-SHAPED and at least 6 long: letters AND digits (or a symbol), never a plain word
        if len(val) < 6 or _PROSE_WORDS.match(val) or val.startswith(("/", "~/", "./", "../")):
            return False
        if not (re.search(r"[A-Za-z]", val) and re.search(r"[0-9!@#%^&*?+=_~]", val)):
            return False
        return not _name_like(val) and shannon(val) >= 2.3
    if len(val) < (8 if quoted else 10):
        return False
    if re.fullmatch(r"[0-9:.,]+", val) or re.fullmatch(r"(?i)true|false|null|none|required", val):
        return False                      # ports, numbers, booleans
    if val.startswith(("/", "~/", "./", "../")):
        return False                      # a path to where the secret lives, not the secret
    if _name_like(val):
        return False                      # a resource / variable NAME, not a value
    if rule == "cli-secret-flag" and flag == "-p":
        return _strong(val)
    return shannon(val) >= 2.5


# PEM private key: only a REAL block is redacted. A block is the BEGIN header, then base64 body
# lines (plus RFC 1421 `Proc-Type:`/`DEK-Info:` headers and blank lines in an encrypted key), then
# END. The old rule also took an UNTERMINATED header "to EOF", so a runbook that merely MENTIONED
# `-----BEGIN OPENSSH PRIVATE KEY-----` in prose lost everything after that sentence.
PEM_BEGIN = re.compile(r"-----BEGIN ([A-Z ]*)PRIVATE KEY(?: BLOCK)?-----")
PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")
_B64_LINE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_PEM_HDR = re.compile(r"^(?:Proc-Type|DEK-Info|Comment|Version|Hash):|^=[A-Za-z0-9+/]{4}$")   # + PGP armor


def redact_pem(text: str, findings: list, fname: str, redact: bool) -> str:
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        m = PEM_BEGIN.search(lines[i])
        # header must END its line (modulo spaces): body on the same line is pem-inline's job
        if not m or lines[i][m.end():].strip():
            out.append(lines[i])
            i += 1
            continue
        j, body, end = i + 1, 0, None
        while j < len(lines):
            t = lines[j].strip()
            if PEM_END.search(t):
                end = j
                break
            if _B64_LINE.match(t):
                body += 1
            elif not (_PEM_HDR.match(t) or (t == "" and body == 0 and j > i + 1)):
                break
            j += 1
        if end is None:
            # unterminated (truncated paste): take the header plus the run of base64-looking lines
            # right after it, stopping at the first blank or non-base64 line. Short lines only
            # count as body after a full-width line, so a one-word prose line never does.
            j, body = i + 1, 0
            while j < len(lines):
                t = lines[j].strip()
                if not _B64_LINE.match(t) or (len(t) < 16 and not (body and t.endswith("="))):
                    break
                body += 1
                j += 1
            end = j - 1
        if body == 0:
            out.append(lines[i])  # a bare header mention in prose: leave it
            i += 1
            continue
        head = lines[i][:m.start()]
        tail = lines[end][PEM_END.search(lines[end]).end():] if PEM_END.search(lines[end]) else ""
        block = "\n".join([lines[i][m.start():]] + lines[i + 1:end + 1])
        mk = marker("pem-private-key", block)
        findings.append(("pem-private-key", fname, i + 1, mk))
        if redact:
            out.append(head + mk + tail)
        else:
            out.extend(lines[i:end + 1])
        i = end + 1
    return "\n".join(out)


# ── entropy fallback ────────────────────────────────────────────────────────────────────────────
# Strict base64 (padding only at the end) OR base64url. `=` must NOT be a mid-token character:
# it merged `VAR=value` into one token and redacted the variable NAME along with the value.
B64_TOKEN = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")   # one class: base64url mixed with -/_ stays ONE token
# A run that mixes `/` with `-`/`_` is a path (cloud resource ids, file paths), not base64url: score its pieces
# the way the split classes did, so a path is judged segment by segment.
_B64_SPLIT = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}|[A-Za-z0-9_-]{16,}")


def _b64_candidates(line: str):
    for m in B64_TOKEN.finditer(line):
        tok = m.group(0)
        if "/" in tok and ("-" in tok or "_" in tok):
            for sm in _B64_SPLIT.finditer(tok):
                yield m.start() + sm.start(), m.start() + sm.end(), sm.group(0)
        else:
            yield m.start(), m.end(), tok
HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{20,}\b")
# Length-normalised: a string of n chars has at most log2(n) bits/char, so the old fixed 4.5 (the
# detect-secrets default, tuned for 40+ char keys) could never fire under ~23 chars. A token must
# reach B64_RATIO of its own maximum, capped at B64_CAP for long tokens, AND mix upper, lower and
# digits. Next to a secret keyword the bar drops (B64_NEAR_RATIO, 16+ chars, a digit plus a letter).
B64_MIN_LEN, B64_RATIO, B64_CAP = 20, 0.85, 4.5
B64_NEAR_MIN_LEN, B64_NEAR_RATIO = 16, 0.75
NEAR_KEYWORD = re.compile(r"(?i)(?:key|token|secret|passw|pwd|auth|credential|\s-p\b).{0,20}$")
HEX_THRESHOLD = 3.0
# Identity-provider object ids (`prefix_` + 16 alphanumerics): identifiers, not secrets
IDP_OBJECT_ID = re.compile(r"^(?:con|cgr|org|rol|cred|lnk)_[A-Za-z0-9]{16}$")
# a public identifier named as such (`APP_CLIENT_ID=...`, `appId: ...`) is not a secret
ID_KEY = re.compile(r"(?i)(?:client|app|application|tenant|object|subscription)[_-]?id[\"']?\s*[:=]\s*[\"']?$")
UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def shannon(s: str) -> float:
    if not s:
        return 0.0
    freq = {c: s.count(c) for c in set(s)}
    return -sum(n / len(s) * math.log2(n / len(s)) for n in freq.values())


def marker(rule: str, value: str) -> str:
    return f"<REDACTED:{rule}:sha256/{hashlib.sha256(value.encode()).hexdigest()[:8]}>"


URL_SPAN = re.compile(r"https?://\S+")


def _wordy(seg: str) -> bool:
    """A segment reads as words, not noise: short, or made of few case/digit runs. `CamelCaseName`,
    `D8sv5`, `oauth2`, `README` and `7f3a9c21` are wordy; `Q7xW2kP9mZ4tR8vB` (16 runs in 16 chars)
    is not."""
    runs = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+", seg)
    return len(seg) <= 8 or len(runs) * 3 <= len(seg)


def _looks_benign(tok: str) -> bool:
    """High-entropy-looking tokens that are NOT secrets: UUIDs, identifiers and paths whose every
    `/ _ -` segment reads as words (cloud SKUs `Standard_D8s_v5`, cloud resource paths, header names,
    file stems, CamelCase names), and base64 that decodes to prose."""
    if UUID.match(tok) or IDP_OBJECT_ID.match(tok):
        return True
    if all(_wordy(s) for s in re.split(r"[/_-]+", tok) if s):
        return True
    if re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", tok) and len(tok) % 4 in (0, 2, 3):
        try:
            raw = base64.b64decode(tok + "=" * (-len(tok) % 4), validate=True)
            txt = raw.decode("ascii")
            if " " in txt and all(32 <= ord(c) < 127 or c in "\n\t" for c in txt):
                return True               # documented example: `echo dGhpcyBp... | base64 -d`
        except (ValueError, UnicodeDecodeError):
            pass
    return False


def b64_entropy_hits(line: str):
    """Base64-charset tokens with high entropy: redactable (unlabeled keys, sig blobs).
    Yields (start, end, token) so the caller can replace by OFFSET: a global str.replace
    corrupted legitimate URLs that happened to contain the same token elsewhere on the line."""
    # Exempt only tokens whose span lies INSIDE a URL span. The old rule ("anything after the
    # first 'http' on the line") exempted a secret merely SHARING a line with a chat link,
    # the most common shape of a real KB line. Secrets inside URLs are still covered by the
    # sas-token / connection-string / keyed-assignment rules, which run regardless.
    urls = [(u.start(), u.end()) for u in URL_SPAN.finditer(line)]
    for m_start, m_end, tok in _b64_candidates(line):
        if "REDACTED" in line[max(0, m_start - 10):m_end + 1]:
            continue
        # skip pure hex here (handled report-only below)
        if re.fullmatch(r"[0-9a-fA-F]+", tok):
            continue
        if any(s <= m_start and m_end <= e for s, e in urls):
            continue
        # a token glued to a word on its left is the tail of a longer identifier / path piece
        if m_start and (line[m_start - 1].isalnum() or line[m_start - 1] in "~."):
            continue
        if ID_KEY.search(line[max(0, m_start - 32):m_start]):
            continue
        classes = sum(bool(re.search(rx, tok)) for rx in (r"[A-Z]", r"[a-z]", r"[0-9]"))
        near = bool(NEAR_KEYWORD.search(line[max(0, m_start - 40):m_start]))
        n = len(tok)
        if near:
            ok = n >= B64_NEAR_MIN_LEN and classes >= 2 and re.search(r"[0-9]", tok) and \
                shannon(tok) >= min(B64_NEAR_RATIO * math.log2(n), B64_CAP)
        else:
            ok = n >= B64_MIN_LEN and classes == 3 and \
                shannon(tok) >= min(B64_RATIO * math.log2(n), B64_CAP)
        if near and not re.search(r"[A-Z]", tok) and "-" in tok:
            ok = False                    # kebab identifier next to a keyword: a pod / object name
        if ok and not _looks_benign(tok):
            yield m_start, m_end, tok


def hex_entropy_hits(line: str):
    """Hex tokens with high entropy, REPORT ONLY: git SHAs and image digests are random hex
    and indistinguishable from keys; auto-redacting them would corrupt legitimate content."""
    for m in HEX_TOKEN.finditer(line):
        if shannon(m.group(0)) >= HEX_THRESHOLD:
            yield m.start(), m.group(0)


YAML_BLOCK_KEY = re.compile(r"(?i)^[ \t-]*[\w.-]*(?:" + KEYWORDS + r")[\"']?\s*:\s*[|>][-+]?[ \t]*$")


def redact_yaml_blocks(text: str, findings: list, fname: str, redact: bool) -> str:
    """`password: |` with the value on the next, more-indented line(s): the per-line rules never see
    the key and the value together."""
    lines = text.split("\n")
    for i, line in enumerate(lines[:-1]):
        if PRAGMA in line or not YAML_BLOCK_KEY.match(line):
            continue
        indent = len(line) - len(line.lstrip())
        j = i + 1
        while j < len(lines) and lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) > indent:
            val = lines[j].strip()
            if " " not in val and "REDACTED" not in val and _value_ok("keyed-assignment", "", val, True):
                mk = marker("yaml-block-secret", val)
                findings.append(("yaml-block-secret", fname, j + 1, mk))
                if redact:
                    lines[j] = lines[j][:len(lines[j]) - len(lines[j].lstrip())] + mk
            j += 1
    return "\n".join(lines)


def process_text(text: str, findings: list, fname: str, redact: bool) -> str:
    # multi-line PEM blocks first (pragma can't protect these: a real key block is never a doc example)
    text = redact_pem(text, findings, fname, redact)
    text = redact_yaml_blocks(text, findings, fname, redact)

    out_lines = []
    for ln, line in enumerate(text.split("\n"), 1):
        if PRAGMA in line:
            out_lines.append(line)
            continue
        new = line
        for rule, rx in RULES:
            def sub(m, rule=rule):
                gi = next(i for i in range(1, (m.lastindex or 0) + 1)
                          if m.group(i) is not None and not (rule == "cli-secret-flag" and i == 1))
                val = m.group(gi)
                # never re-match inside an existing <REDACTED:...> marker (idempotency):
                # marker text like "bearer-token:sha256/a52779e7" looks like a keyed assignment
                if "REDACTED" in m.string[max(0, m.start() - 24):m.end() + 1]:
                    return m.group(0)
                if rule == "license-key" and not _key_ok(val):
                    return m.group(0)
                if rule in ("keyed-assignment", "cli-secret-flag", "prose-password", "short-password",
                            "table-secret"):
                    flag = m.group(1) if rule == "cli-secret-flag" else ""
                    quoted = m.string[m.start(gi) - 1:m.start(gi)] in ("\"", "'")
                    if not quoted:
                        # sentence punctuation is not part of an unquoted value
                        trimmed = val.rstrip(".,;:)]}>")
                        if trimmed != val:
                            val = trimmed
                    if not _value_ok(rule, flag, val, quoted):
                        return m.group(0)
                mk = marker(rule, val)
                findings.append((rule, fname, ln, mk))
                if not redact:
                    return m.group(0)
                a = m.start(gi) - m.start()
                return m.group(0)[:a] + mk + m.group(0)[a + len(val):]
            new = rx.sub(sub, new)
        # replace right-to-left by offset so earlier spans stay valid and identical tokens
        # elsewhere on the line (e.g. inside a URL) are never touched
        for start, end, tok in sorted(b64_entropy_hits(new), reverse=True):
            mk = marker("entropy-b64", tok)
            findings.append(("entropy-b64", fname, ln, mk))
            if redact:
                new = new[:start] + mk + new[end:]
        for _, tok in hex_entropy_hits(new):
            # report-only: never redacted (git SHAs / digests are legitimate high-entropy hex)
            findings.append(("entropy-hex(report-only)", fname, ln,
                             f"sha256/{hashlib.sha256(tok.encode()).hexdigest()[:8]}"))
        out_lines.append(new)
    return "\n".join(out_lines)


# ── self-test ───────────────────────────────────────────────────────────────────────────────────
# `leyden-redact.py --selftest`: every shape the redactor must catch, and every legitimate KB line it
# must leave alone. Run it after touching any rule; exit 0 = all pass. All values are fake.
_PEM_BODY = "\n".join(["b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW",
                       "QyNTUxOQAAACDx3v2Fh8qk0Zz3L9mXr5Tq1Wc7Yb2Nd4Ue6Gf8Hj0KlAAAAJgAAAAAAAAA",
                       "AAtzc2gtZWQyNTUxOQAAACDx3v2Fh8qk0Zz3L9mXr5Tq1Wc7Yb2Nd4Ue6Gf8Hj0KlA=="])
# (text, rule that must fire, substring that must NOT survive redaction)
SELFTEST_POSITIVE = [
    ("https://bucket.s3.amazonaws.com/f?X-Amz-Algorithm=AWS4&X-Amz-Signature=9f86d081884c7d659a2feaa0c55ad015", "sas-token", "9f86d081884c7d65"),
    ("https://storage.googleapis.com/b/f?X-Goog-Signature=Ab3dE5gH7jK9mN1pQ2rS4tU6vW8", "sas-token", "Ab3dE5gH7jK9mN1p"),
    ("aws configure set aws_secret_access_key wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "entropy-b64", "wJalrXUtnFEMI"),
    ("token ya29." "a0AfH6SMBx7kQ2vZ3wR5tPq8nL4bY7cD1eF6gH0jK2aB3cD", "google-oauth-token", "a0AfH6SMBx7kQ2"),
    ("SENDGRID SG." "Ab3dE5gH7jK9mN1pQ2rS4t.x9Yz7Kq2LmZ8wP4Rt6uV8wX0yZ2aB4cD6eF8gH0iJ", "sendgrid-key", "x9Yz7Kq2LmZ8"),
    ("refresh v1.MRrT7kXq9Lm2Pz8Wv4Rt6uY8wX0yZ2aB4cD6eF8gH0iJ1kL3mN5", "refresh-token", "MRrT7kXq9Lm2"),
    ("hook https://hooks.slack.com/" "services/T0ABC1234/B0DEF5678/Xk9mQ2vZ3wR5tPq8nL4bY7cD", "slack-webhook", "Xk9mQ2vZ3wR5"),
    ("id 1a2B3c4D5e6F7g8H-9i0J1k2L3m4N5o6P7q8R9s0T here", "entropy-b64", "9i0J1k2L3m4N"),
    ("mysql -uroot -pHx7kq2Lmz9 -h db", "cli-secret-flag", "Hx7kq2Lmz9"),
    ("| password | Hx7kq2Lmz9Pw |", "table-secret", "Hx7kq2Lmz9Pw"),
    ("password: |\n  Hx7kq2Lmz9Pw4\nnext: 1", "yaml-block-secret", "Hx7kq2Lmz9Pw4"),
    ("the password for the vm is Hx7kq2Lm", "prose-password", "Hx7kq2Lm"),
    ("secret=Hx7kq2Lm9", "short-password", "Hx7kq2Lm9"),
    ("-----BEGIN PGP PRIVATE KEY BLOCK-----\nVersion: GnuPG v2\n\n" + "lQOYBF2xQ3kBCADKj8sT0h7Fz9Ql3mW2pRvXcY5nB8tA1dE4gH6jK9mL0oP2qS5u\n" * 3
     + "=Ab3d\n-----END PGP PRIVATE KEY BLOCK-----", "pem-private-key", "lQOYBF2xQ3kB"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\n" + _PEM_BODY + "\n-----END OPENSSH PRIVATE KEY-----\nafter",
     "pem-private-key", "b3BlbnNzaC1rZXkt"),
    ("    -----BEGIN RSA PRIVATE KEY-----\n    Proc-Type: 4,ENCRYPTED\n    DEK-Info: AES-128-CBC,AB12\n\n    "
     + _PEM_BODY.replace("\n", "\n    ") + "\n    -----END RSA PRIVATE KEY-----", "pem-private-key", "QyNTUxOQAAACDx"),
    ("truncated paste:\n-----BEGIN PRIVATE KEY-----\n" + _PEM_BODY + "\n\nnext paragraph",
     "pem-private-key", "QyNTUxOQAAACDx"),
    ('{"key": "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\\n-----END PRIVATE KEY-----"}',
     "pem-inline", "MIIEvQIBADANBgkq"),
    ("OPENAI_API_KEY=sk-proj-Ab3dE5gH7jK9mN1pQ2rS4tU6vW8xY0zAb3dE5gH7", "openai-key", "Ab3dE5gH7jK9"),
    ("service key sk-qx7Tn4w9RjB2mF8sV5cD1hK3", "openai-key", "qx7Tn4w9"),
    ("token glpat-" "Ab3dE5gH7jK9mN1pQ2rS", "gitlab-token", "Ab3dE5gH7jK9"),
    ("//registry.npmjs.org/:_authToken=npm_" "Ab3dE5gH7jK9mN1pQ2rS4tU6vW8xY0zAb3dE", "npm-token", "Ab3dE5gH7jK9"),
    ("maps AIza" "SyAb3dE5gH7jK9mN1pQ2rS4tU6vW8xY0zAb", "google-api-key", "SyAb3dE5"),
    ("STRIPE_KEY=sk_live_" "aaaaaaaaaaaaaaaaaaaaaaaa", "stripe-key", "aaaaaaaaaaaa"),
    ("test key sk_test_" "4eC39HqLyjWDarjtT1zdp7dc", "stripe-key", "4eC39HqLyj"),
    ("restricted rk_live_" "51HqLyjWDarjtT1zdp7dc", "stripe-key", "51HqLyjWDar"),
    ("DB_PASS=hunter22x", "short-password", "hunter22x"),
    ("MYSQL_PWD=hunt3r!x", "short-password", "hunt3r!x"),
    ("pass: Tr0ub4dor9", "short-password", "Tr0ub4dor9"),
    ("curl -H 'Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==' https://api", "basic-auth", "dXNlcjpw"),
    ("REDIS_URL=redis://:S3cr3tPassw0rd@cache:6379/0", "connection-string", "S3cr3tPassw0rd"),
    ("postgres://dbadmin:Pg-Adm1n-2024!@db.internal:5432/app", "connection-string", "Pg-Adm1n-2024!"),
    ("password=P@ssw0rd!2024xx", "keyed-assignment", "P@ssw0rd!2024xx"),
    ('password: "Tr0ub4dor&3xkcd!"', "keyed-assignment", "Tr0ub4dor&3xkcd!"),
    ("db_pwd: 'correct horse 42!'", "keyed-assignment", "correct horse 42!"),
    ("cli login -u app-id -p Xy7kL9mQ2vZ3wR5t --org example", "cli-secret-flag", "Xy7kL9mQ2vZ3wR5t"),
    ("mysql --password 'Hunter2Hunter2!' -h db", "cli-secret-flag", "Hunter2Hunter2!"),
    ("storage-cli ls --account-key=Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3Bx==",
     "cli-secret-flag", "Zm9vYmFyQmF6"),
    ("app create --client-secret Kx9-Ab3dE5gH7jK9mN1pQ2r", "cli-secret-flag", "Kx9-Ab3dE5gH7"),
    ("CLIENT_SECRET=abc8Q~xYz.Ab-CdEfGhIjKlMnOpQrStUvWxYz012", "azure-client-secret", "abc8Q~xYz"),
    ("client_secret: Zq18Q~Ab3dE5gH7jK9mN1pQ2rS4tU6vW8xY0z_.A", "azure-client-secret", "Zq18Q~"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9xyzABCDEFGH123456", "bearer-token", "eyJhbGciOiJIUzI1NiJ9"),
    ("aws AKIAIOSFODNN7EXAMPLE", "aws-key-id", "AKIAIOSFODNN7EXAMPLE"),
    ("gh ghp_" "Ab3dE5gH7jK9mN1pQ2rS4tU6vW8xY0z", "github-token", "ghp_Ab3dE5gH7"),
    ("slack xoxb-" "1234567890-Ab3dE5gH7jK9", "slack-token", "xoxb-1234567890"),
    ("https://acct.blob.core.windows.net/c/f?sv=2022&sig=Ab3dE5gH7jK9mN1pQ2rS4t%2BU6vW8", "sas-token", "Ab3dE5gH7jK9mN1p"),
    ("unlabeled Zk3vQ9pL2mX8rT4wB7nY", "entropy-b64", "Zk3vQ9pL2mX8rT4wB7nY"),
    ("the token is Ab3dE5gH7jK9mN1p", "entropy-b64", "Ab3dE5gH7jK9mN1p"),
    ("the admin password is Pa55w0rd!Xq9 for now", "prose-password", "Pa55w0rd!Xq9"),
    ("pwd = hunter22", "short-password", "hunter22"),
    ("Password was Kx7#mq2Z after the reset", "prose-password", "Kx7#mq2Z"),
    ("product key AB-7F3A-99QX-K2M8-PL4Z for the box", "license-key", "7F3A-99QX"),
]
# lines that must survive byte-for-byte with no hard finding
SELFTEST_NEGATIVE = [
    "| password | stored in the vault |",
    "| api key | see 1Password item `prod-api` |",
    "path src/components/user-profile/settings_panel.tsx and docs/how-to/rotate_certs.md",
    "mysql -u root -p -h db",
    "description: >\n  The password is kept in the vault, ask the owner.",
    "secret: |\n  see the vault entry prod-db",
    "The key file starts with -----BEGIN OPENSSH PRIVATE KEY----- and that header is fine to mention.\n"
    "This paragraph after it must survive.\nSo must this one.",
    "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----\nThen paste the body here.",
    "existingSecret: app-postgres-credentials",
    "passwordSecret: app-db-admin-password",
    "Pull secret: app-registry-pull-secret-prod",
    "secret: aws_secret_access_key",
    "secretName: app-k8s-tls-v2",
    "tokenSecretRef: /etc/secrets/db-password",
    "password: ${DB_PASSWORD}",
    "password: <your-password-here>",
    "client_secret: ************",
    "token_count: 1234567890123",
    "password: required",
    "cli login --client-secret $CLIENT_SECRET",
    "mkdir -p /opt/app/data/shared-volume",
    "docker run -p 8080:80 nginx-latest-build",
    "ssh -p 2222 dev@vm-01.example.com",
    "mkdir -p tmp/build-fix",
    "request id 550e8400-e29b-41d4-a716-446655440000 and 550E8400-E29B-41D4-A716-446655440000",
    "commit 7d1e4a9b2c5f8e3a6d0b9c2e5f8a1d4b7c0e3f6a9b2c merged",
    "digest sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "registry.example.com/app@sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "/subscriptions/12345678-1a2b-4c3d-8e9f-0123456789ab/resourceGroups/test-rg/providers/"
    "Microsoft.Compute/virtualMachines/dev-vm-123",
    "arn:aws:iam::123456789012:role/app-prod01-deploy and projects/app-prod-01/zones/zone-b/instances/vm01",
    "pod web-api-7d9f8b6c5-x2v4q CrashLoopBackOff; secret mounted on app-worker-5f6b7c8d9-qz7kp",
    "echo dGhpcyBpcyBhIHRlc3Qgb2YgcHJvc2U= | base64 -d",
    "image app:1.4.0-alpine on product 3.2.0rc1, ticket ABC-123",
    "postgres://dbadmin@db.internal:5432/app",
    "git@github.com:example-org/docs.git",
    "see https://github.com/org/repo/blob/main/README.md for scikit-learn and sk-learn notes",
    "fake example api_key=Ab3dE5gH7jK9mN1pQ2rS4tU6 # pragma: allowlist secret",
    "getUserByIdAsyncHandlerFactory and ThisIsALongJavaClassName",
    "already redacted: password=<REDACTED:keyed-assignment:sha256/1d6251cd>",
    "node pool pool1 (`Standard_D8s_v5`, `m5.2xlarge`, `n2-standard-8`), file Acme_architecture_v2.pdf",
    "authResponseHeaders=X-Forwarded-User; add imagePullSecrets",
    "issuer=$HOSTNAME/oauth2/app and app `SampleWebApp`",
    "IdP connection `con_Q7xW2kP9mZ4tR8vB` disabled",
    "APP_CLIENT_ID=Zq8vLm3TrW6yHc1NbX4pKd7FsJ2gQe9U and appId: 3f2b9c1e7d4a",
    "# Docker Password = Xq4M******b7Pw",
    "api_key:  os.environ/LLM_PROVIDER_API_KEY   # the key to reach it",
    "session_token = fetch_service_token(access_token=token_var, strategy='default')",
    """kubectl patch ds example-agent -n tools --type='json' -p='[{"op": "add", "path": "/spec/x"}]'""",
    "the password is stored in the vault, the admin password was reset yesterday",
    "pwd = $PWD and password: required",
    "ticket ABC-123 and XYZ-4821, SKU STANDARD-D8S-V5-XL",
    "request 550E8400-E29B-41D4-A716-446655440000",
    "password is changed",
    "DB_PASSWORD_FILE=/run/secrets/db-password and SKIP_PASS=true, bypass: enabled",
    "pk_live_ publishable prefix and sk_live_ alone in prose",
]


def selftest() -> int:
    failures = []
    for text, rule, secret in SELFTEST_POSITIVE:
        f = []
        out = process_text(text, f, "t", redact=True)
        if secret in out:
            failures.append(f"MISSED [{rule}] {secret!r} survived in: {out!r}")
        if rule not in {x[0] for x in f}:
            failures.append(f"RULE [{rule}] did not fire (got {sorted({x[0] for x in f})}) for: {text[:70]!r}")
        f2 = []
        if process_text(out, f2, "t", redact=True) != out or [x for x in f2 if not x[0].startswith("entropy-hex")]:
            failures.append(f"NOT IDEMPOTENT [{rule}]: {out!r} -> {f2}")
    for text in SELFTEST_NEGATIVE:
        f = []
        out = process_text(text, f, "t", redact=True)
        hard = [x[0] for x in f if not x[0].startswith("entropy-hex")]
        if out != text or hard:
            failures.append(f"FALSE POSITIVE {hard}: {text[:90]!r} -> {out[:120]!r}")
    for msg in failures:
        print(msg)
    total = len(SELFTEST_POSITIVE) + len(SELFTEST_NEGATIVE)
    print(f"leyden-redact selftest: {total - len(failures)}/{total} checks passed"
          if not failures else f"leyden-redact selftest: {len(failures)} FAILURE(S)")
    return 1 if failures else 0


def main():
    args = sys.argv[1:]
    if args[:1] == ["--selftest"]:
        return selftest()
    if not args or args[0] not in ("--scan", "--redact"):
        print(__doc__)
        return 2
    redact = args[0] == "--redact"
    findings = []
    for target in args[1:]:
        if target == "-":
            process_text(sys.stdin.read(), findings, "<stdin>", redact=False)
            continue
        p = Path(target)
        if not p.is_file():
            continue
        try:
            raw = p.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: caller keeps its quarantine path for these
        new = process_text(raw, findings, str(p), redact)
        if redact and new != raw:
            p.write_text(new, encoding="utf-8")
    hard = [f for f in findings if not f[0].startswith("entropy-hex")]
    for rule, fname, ln, mk in findings:
        print(f"{rule}\t{fname}:{ln}\t{mk}")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
