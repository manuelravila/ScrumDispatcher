"""Muse feed (phase 2, TASK2 §5): the only text Meta Muse is ever allowed to see.

Muse is external — dispatcher.run_muse hands tools/muse the raw message with no thread context — so a
Muse test brief must carry no code, no file paths, no diffs, no credentials and no client data: just the
card title, the public target URL, numbered steps and the expected result.

The three steps a caller uses:
- extract_test_brief(description): the TEST-BRIEF ... END-TEST-BRIEF section the specialist wrote, else None.
  It fails closed — a missing or unterminated marker yields None, so the card waits in To test and a partial
  brief is never shipped to Meta's servers.
- sanitize_brief(text): defence in depth over whatever the specialist actually wrote.
- build_muse_message(title, url, brief): title + URL + numbered steps + expected result and nothing else;
  raises ValueError instead of producing a message Muse cannot act on.

URL policy: allow_hosts is a tuple of hosts (company.json `muse_targets` hosts, filled by Ringo). A URL
whose host is not in it is stripped; subdomains of an allowed host count as allowed. build_muse_message
takes allow_hosts=None by default: the target URL comes from company.json and is validated as a URL, not
against the brief allowlist — pass the tuple when the target should be checked too.
"""
import re
from urllib.parse import urlsplit

BRIEF_START = 'TEST-BRIEF'
BRIEF_END = 'END-TEST-BRIEF'
REMOVED = '[removed]'
ALLOWED_HOSTS = ()

# (?<!END-) so the END marker is never read as a start marker. First complete block wins.
_BRIEF_RE = re.compile(r'(?<!END-)\bTEST-BRIEF\b(.*?)\bEND-TEST-BRIEF\b', re.I | re.S)

_FENCE_RE = re.compile(r'^(?:```|~~~|\+\+\+|---)')
_DIFF_RE = re.compile(r'^(?:diff --git |index [0-9a-f.]{6,40}(?: |$)|new file mode \d|deleted file mode \d'
                      r'|old mode \d|new mode \d|similarity index \d|rename (?:from|to) |copy (?:from|to) '
                      r'|@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@|[+-]{3} \S|[+-]\S)', re.I)
_PRIVATE_KEY_BEGIN_RE = re.compile(r'^-{3,}BEGIN [A-Z ]*PRIVATE KEY')
_PRIVATE_KEY_END_RE = re.compile(r'^-{3,}END [A-Z ]*PRIVATE KEY')
_URL_RE = re.compile(r'\b(?:https?://|www\.)[^\s<>"\']+')
_EMAIL_RE = re.compile(r'\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b')
_INLINE_CODE_RE = re.compile(r'`[^`\n]+`')
_PATH_EXT = (r'py|pyc|js|jsx|ts|tsx|vue|json|ya?ml|toml|ini|cfg|md|txt|sh|bash|sql|sqlite3?|env|ini|html|htm'
             r'|css|lock|log|csv|tsv|ipynb|docx?|xlsx?|pptx?|pdf|diff|patch|tar|gz|zip|woff2?|svg')
# One alternation: an allowed URL is consumed whole, so its path is not re-scanned as a file path.
_SCRUB_RE = re.compile(
    r'(?P<url>' + _URL_RE.pattern + r')'
    r'|(?P<email>' + _EMAIL_RE.pattern + r')'
    r'|(?P<secret>-----END [A-Z ]*PRIVATE KEY-----'
    r'|\b(?:sk|pk|ghp|gho|ghu|ghs|ghr|xox[abprs]|AKIA|AIza|ya29|notion|npm|pypi|glpat|dop_pat)[-_][A-Za-z0-9_\-]{6,}\b'
    r'|\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\b'
    r'|\bBearer\s+[A-Za-z0-9._\-]{8,}\b'
    r'|\b[A-Za-z][A-Za-z0-9_]*_(?:SECRET|KEY|TOKEN|PASSWORD|PASSPHRASE|SALT|APIKEY|API_KEY|CREDENTIAL)'
    r'[A-Za-z0-9_]*\s*[:=]\s*\S+'
    r'|\b(?:password|passphrase|passwd|secret|token|api[ _-]?key)\s*[:=]\s*\S+\b'
    r'|\b[A-Za-z0-9+/]{24,}={0,2}\b'
    r'|\b\d{9,}\b|\b\d{4}(?:[ -]\d{4}){2,}\b)'
    r'|(?P<path>(?:[A-Za-z]:[\\/]|[~/]\.?[/\\]|/(?:opt|etc|var|home|root|usr|srv|Users|private|tmp)/'
    r'|(?:(?:\.{1,2}|[\w.\-]+)[/\\])+)(?=\S*[A-Za-z])[^\s]*'
    r'|\b[\w.\-]+\.(?:' + _PATH_EXT + r')\b)'
    r'|(?P<code>`[^`\n]+`)', re.I)

_STEP_MARKER_RE = re.compile(r'^\s*(?:\d+[.)]|[a-z][.)]|[-*••])\s*')
_EXPECTED_KEYS = ('expected result', 'expected outcome', 'expected behaviour', 'expected behavior',
                  'expected', 'success criteria', 'acceptance criteria')
_BLANK_RUN_RE = re.compile(r'\n{3,}')


def extract_test_brief(description):
    """The text between TEST-BRIEF and END-TEST-BRIEF, or None when there is no complete section."""
    if not description:
        return None
    m = _BRIEF_RE.search(description)
    if not m:
        return None
    brief = m.group(1).strip().lstrip(':').strip()
    return brief or None


def _host_set(allow_hosts):
    hosts = set()
    for h in allow_hosts or ():
        h = str(h).strip().lower().lstrip('.')
        if h.startswith('www.'):
            h = h[4:]
        if h:
            hosts.add(h)
    return hosts


def _host_of(url):
    raw = url if '://' in url else 'http://' + url
    host = urlsplit(raw).hostname or ''
    host = host.lower()
    return host[4:] if host.startswith('www.') else host


def _url_allowed(url, hosts):
    host = _host_of(url)
    return any(host == h or host.endswith('.' + h) for h in hosts)


def _trim_url(url):
    return url.rstrip('.,;:!?')


def _scrub_line(line, hosts):
    def repl(m):
        kind = m.lastgroup
        if kind == 'url':
            url = _trim_url(m.group('url'))
            return url if _url_allowed(url, hosts) else REMOVED
        return REMOVED
    line = _SCRUB_RE.sub(repl, line)
    line = _INLINE_CODE_RE.sub(REMOVED, line)
    line = re.sub(r'(?:\s*' + re.escape(REMOVED) + r')+', ' ' + REMOVED, ' ' + line)
    return line.strip()


def sanitize_brief(text, allow_hosts=ALLOWED_HOSTS):
    """Strip code fences, inline code, diffs, file paths, emails, credentials/account numbers and any URL
    whose host is not in allow_hosts. Returns '' for an empty/fully-stripped brief."""
    if not text:
        return ''
    hosts = _host_set(allow_hosts)
    out, fenced, key_block = [], False, False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('```') or line.startswith('~~~'):
            fenced = not fenced
            continue
        if _PRIVATE_KEY_BEGIN_RE.match(line):
            key_block = True      # an unterminated key block swallows the rest of the brief: never to Muse
            continue
        if key_block:
            if _PRIVATE_KEY_END_RE.match(line):
                key_block = False
            continue
        if fenced or _DIFF_RE.match(line):
            continue
        if not line:
            out.append('')
            continue
        cleaned = _scrub_line(line, hosts)
        if cleaned and cleaned != REMOVED:
            out.append(cleaned)
    return _BLANK_RUN_RE.sub('\n\n', '\n'.join(out)).strip()


def _is_expected_line(line):
    """None if the line is a step; otherwise the text after the expected-result keyword ('' if none)."""
    s = line.strip().lstrip('#>-* \t').strip()
    low = s.lower()
    for key in sorted(_EXPECTED_KEYS, key=len, reverse=True):
        if low.startswith(key):
            return s[len(key):].strip(' \t:–—-')
    return None


def parse_brief(brief):
    """(steps, expected) from a sanitized brief. expected is None when the brief states none; the
    expected block runs from its keyword to the end of the brief."""
    steps, expected = [], []
    lines = [line.strip() for line in (brief or '').splitlines()]
    idx = 0
    while idx < len(lines):
        tail = _is_expected_line(lines[idx])
        if tail is None:
            if lines[idx]:
                steps.append(_STEP_MARKER_RE.sub('', lines[idx]).strip())
            idx += 1
            continue
        expected.append(tail)
        idx += 1
        while idx < len(lines):
            if lines[idx]:
                expected.append(_STEP_MARKER_RE.sub('', lines[idx]).strip())
            idx += 1
        break
    steps = [s for s in steps if s]
    return steps, (' '.join(e for e in expected if e).strip() or None)


def _validate_target(target_url, allow_hosts):
    url = str(target_url or '').strip()
    if not url or any(c.isspace() for c in url):
        raise ValueError('target_url must be a single URL')
    if not url.lower().startswith(('http://', 'https://')):
        raise ValueError(f'target_url must be http(s): {url!r}')
    host = _host_of(url)
    if not host or '.' not in host:
        raise ValueError(f'target_url has no usable host: {url!r}')
    hosts = _host_set(allow_hosts)
    if allow_hosts is not None and not _url_allowed(url, hosts):
        raise ValueError(f'target host {host} is not in the allowlist')
    return url


def build_muse_message(card_title, target_url, brief, allow_hosts=None):
    """The Muse message: title, URL, numbered steps, expected result — nothing else."""
    title = ' '.join(str(card_title or '').split())
    if not title:
        raise ValueError('card_title is empty')
    title = _scrub_line(title, _host_set(ALLOWED_HOSTS))
    url = _validate_target(target_url, allow_hosts)
    steps, expected = parse_brief(brief)
    if not steps:
        raise ValueError('the Muse brief has no steps')
    if expected is None:
        raise ValueError('the Muse brief has no expected result')
    lines = [f'Test: {title}', f'URL: {url}', '', 'Steps:']
    lines += [f'{n}. {step}' for n, step in enumerate(steps, 1)]
    lines += ['', f'Expected result: {expected}']
    return '\n'.join(lines)
