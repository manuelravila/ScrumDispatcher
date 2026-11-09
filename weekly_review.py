"""Weekly pipeline review card (phase 2, TASK2 §7): pure date logic, no Deck calls.

TASK2: once per week (Monday 08:00 local) the dispatcher creates a card "Weekly pipeline review" in To spec
on board 29 if none open exists; idempotent and durable (no session cron). This module answers only the two
questions the dispatcher needs — is it due, and what does the card say. The Deck call (which board/stack,
which session) stays with the caller; WEEKLY_BOARD / WEEKLY_STACK below are the TASK2 values.

due() and card_payload() take a local datetime: naive is treated as local wall-clock, aware is used as given.
Callers that want a stricter once-a-week guarantee should keep the durable marker themselves — week_key() is
exported for exactly that (a review finished and moved to Done on the same Monday must not spawn a second card).
"""
import datetime as dt
import re
import statistics

WEEKLY_TITLE = 'Weekly pipeline review'
WEEKLY_BOARD = 29          # TASK2: board 29
WEEKLY_STACK = 'To spec'   # TASK2: To spec
WEEKLY_START_HOUR = 8
MAX_IMPROVEMENTS = 3
DAY = 24 * 3600
WEEK = 7 * DAY

_CLOSED_STACK_TITLES = frozenset({'done', 'to done', 'archived'})
_CLOSED_STATUSES = frozenset({'done', 'closed', 'archived', 'cancelled', 'deleted'})


def _require_datetime(now):
    if not isinstance(now, dt.datetime):
        raise TypeError(f'expected a datetime, got {type(now).__name__}')
    return now


def week_key(now):
    """ISO week marker for a durable 'already created this week' flag, e.g. '2026-W45'."""
    year, week, _ = _require_datetime(now).isocalendar()
    return f'{year}-W{week:02d}'


def _norm(title):
    return ' '.join(str(title or '').split()).lower()


def _closed_by_stack(card):
    stack = card.get('stack')
    if isinstance(stack, dict):
        return (_norm(stack.get('title')) in _CLOSED_STACK_TITLES
                or _norm(stack.get('type')) == 'archive')
    if isinstance(stack, str):
        return _norm(stack) in _CLOSED_STACK_TITLES
    return (_norm(card.get('stackTitle')) in _CLOSED_STACK_TITLES
            or _norm(card.get('stackType')) == 'archive')


def is_open(card):
    """A Deck card is open unless it is archived, soft-deleted, or sits in a Done/archive stack. Cards that
    carry only a stackId are taken as open — pass the stacks you actually want to count as open."""
    if not isinstance(card, dict):
        return False
    if card.get('deletedAt'):
        return False
    if card.get('archived'):
        return False
    if _norm(card.get('status')) in _CLOSED_STATUSES:
        return False
    return not _closed_by_stack(card)


def open_weekly_cards(existing_cards):
    """The open cards titled 'Weekly pipeline review' (title matched ignoring case and spacing)."""
    wanted = _norm(WEEKLY_TITLE)
    return [c for c in (existing_cards or ()) if _norm(c.get('title')) == wanted and is_open(c)]


def due(now, existing_cards):
    """True from Monday 08:00 local while no open 'Weekly pipeline review' card exists."""
    now = _require_datetime(now)
    if now.weekday() != 0:
        return False
    if (now.hour, now.minute, now.second) < (WEEKLY_START_HOUR, 0, 0):
        return False
    return not open_weekly_cards(existing_cards)


def card_payload(now, data_block=''):
    """The Deck card body: title plus the checklist the reviewer works through (TASK2 §7, TASK-FREDDY).
    `data_block` is the TASK4 part F LLM-free facts block, appended when the Scrum flag is on."""
    now = _require_datetime(now)
    lines = [
        f'Weekly pipeline review for {week_key(now)} (created {now:%Y-%m-%d %H:%M} local).',
        'Review the whole line: To spec → To do → Doing → To review → To test → Done.',
        '',
        '- [ ] Queue throughput: cards pulled per coder and per project, and unlabelled cards that stayed in To do',
        '- [ ] Escalations: cards relabelled Argon or moved back to To spec, and why each one escalated',
        '- [ ] Failed deploys: each failure, its cause, and what would have caught it earlier',
        '- [ ] Stuck cards: any card with no movement for a day or more, in any stack',
        '- [ ] Muse fails: each failed Muse test, what it found, and the card it came from',
        '- [ ] Improvements: write one card per improvement into To spec — public steps only, no code or client data',
    ]
    if data_block:
        lines += ['', data_block.strip()]
    lines += [
        '',
        f'Created by the dispatcher on Mondays from {WEEKLY_START_HOUR:02d}:00 while no open card titled '
        f'"{WEEKLY_TITLE}" exists. Owner: Ringo.',
    ]
    return {'title': WEEKLY_TITLE, 'description': '\n'.join(lines)}


# ---------------------------------------------------------------- TASK4 part F: LLM-free facts block
def _now_ts(now):
    now = _require_datetime(now)
    return now.timestamp()


def _entry_ts(entry):
    for key in ('at', 'done_at', 'ts', 'time'):
        value = (entry or {}).get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                try:
                    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
                except ValueError:
                    pass
    return 0.0


def _recent(entries, now_ts, window=WEEK):
    return [e for e in (entries or []) if 0 <= now_ts - _entry_ts(e) <= window]


def _duration(seconds):
    if seconds is None:
        return 'n/a'
    seconds = max(0.0, float(seconds))
    if seconds < 3600:
        return f'{seconds / 60:.0f}m'
    if seconds < 2 * DAY:
        return f'{seconds / 3600:.1f}h'
    return f'{seconds / DAY:.1f}d'


def _worker_of(entry):
    return str((entry or {}).get('worker') or (entry or {}).get('agent') or '').lower()


def _project_of(entry):
    return str((entry or {}).get('project') or '')


def _median_cycle(entries):
    values = [float(e['cycle_seconds']) for e in entries
              if isinstance(e.get('cycle_seconds'), (int, float)) and e['cycle_seconds'] >= 0]
    return statistics.median(values) if values else None


def describe_metric(metric, value):
    """Human form of a metric value for approval messages and before/after lines."""
    if value is None:
        return 'n/a'
    if str(metric or '').startswith('cycle:'):
        return _duration(value)
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value)


def _metric_value(state, now_ts, metric):
    """Current 7-day value of a metric key, e.g. returns:project:lucidmail."""
    parts = str(metric or '').split(':', 2)
    if len(parts) != 3:
        return None
    kind, scope, name = parts
    done = _recent(state.get('done_log'), now_ts)
    returns = _recent(state.get('return_log') or state.get('returned_log'), now_ts)
    failures = _recent(state.get('failure_log'), now_ts)
    quotas = _recent(state.get('quota_log'), now_ts)
    if scope == 'worker':
        done = [e for e in done if _worker_of(e) == name.lower()]
        returns = [e for e in returns if _worker_of(e) == name.lower()]
        failures = [e for e in failures if _worker_of(e) == name.lower()]
        quotas = [e for e in quotas if _worker_of(e) == name.lower()]
    elif scope == 'project':
        done = [e for e in done if _project_of(e) == name]
        returns = [e for e in returns if _project_of(e) == name]
        failures = [e for e in failures if _project_of(e) == name]
        quotas = []
    else:
        return None
    if kind == 'returns':
        return len(returns)
    if kind == 'cycle':
        return _median_cycle(done)
    if kind == 'failures':
        return len(failures)
    if kind == 'quota':
        return len(quotas)
    if kind == 'stalled':
        return len([e for e in returns + failures
                    if _STALL_RE.search(str(e.get('reason') or e.get('note') or ''))])
    return None


_STALL_RE = re.compile(r'no heartbeat|stalled|process is gone|no output|silence', re.I)
_CAUSE_RULES = (
    ('stalled/no heartbeat', _STALL_RE),
    ('quota/limit', re.compile(r'quota|rate.?limit|credit|\b402\b|\b429\b|session limit|usage limit', re.I)),
    ('tests/CI failing', re.compile(r'\btests?\b|\bsmoke\b|\bci\b|exit code', re.I)),
    ('review fixes', re.compile(r'review|fix round|fixes|not approved', re.I)),
    ('deploy/merge', re.compile(r'deploy|merge|conflict|integrat', re.I)),
    ('timeout', re.compile(r'timeout|timed out', re.I)),
    ('unready spec', re.compile(r'definition of ready|not ready|missing ', re.I)),
    ('worker error', re.compile(r'error|exception|failed|failure', re.I)),
)


def _cause_label(reason):
    text = str(reason or '')
    for label, rx in _CAUSE_RULES:
        if rx.search(text):
            return label
    return 'other'


def _delay_causes(state, now_ts):
    returns = state.get('return_log')
    if returns is None:
        returns = state.get('returned_log')
    entries = list(_recent(returns, now_ts))
    for key in ('escalation_log', 'failure_log'):
        entries.extend(_recent(state.get(key), now_ts))
    counts = {}
    for entry in entries:
        label = _cause_label(entry.get('reason') or entry.get('note'))
        counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _summarise(name, scope, done, returns, escalations, failures, quotas):
    def _match(entry):
        return (_worker_of(entry) if scope == 'worker' else _project_of(entry)) == name
    mine_done = [e for e in done if _match(e)]
    mine_returns = [e for e in returns if _match(e)]
    mine_esc = [e for e in escalations if _match(e)]
    mine_fail = [e for e in failures if _match(e)]
    mine_quota = [e for e in quotas if _match(e)]
    median = _median_cycle(mine_done)
    return {'done': len(mine_done), 'median': _duration(median),
            'returns': len(mine_returns), 'escalations': len(mine_esc),
            'failures': len(mine_fail), 'quota': len(mine_quota)}


def build_data_block(state, now):
    """Deterministic weekly facts block: per worker and per project, plus the top 3 delay causes.
    Pure: reads STATE and `now` only. No LLM, no network."""
    state = state or {}
    now_ts = _now_ts(now)
    done = _recent(state.get('done_log'), now_ts)
    returns = _recent(state.get('return_log') or state.get('returned_log'), now_ts)
    escalations = _recent(state.get('escalation_log'), now_ts)
    failures = _recent(state.get('failure_log'), now_ts)
    quotas = _recent(state.get('quota_log'), now_ts)

    workers = sorted({_worker_of(e) for e in done + returns + escalations + failures + quotas
                      if _worker_of(e)})
    projects = sorted({_project_of(e) for e in done + returns + escalations + failures
                       if _project_of(e)})

    lines = [f'Weekly data block for {week_key(now)} (last 7 days; built from dispatcher state, no LLM):']
    if workers:
        lines.append('Workers:')
        for worker in workers:
            s = _summarise(name=worker, scope='worker', done=done, returns=returns,
                           escalations=escalations, failures=failures, quotas=quotas)
            lines.append(f"- {worker.capitalize()}: done {s['done']}, median cycle {s['median']}, "
                         f"returns {s['returns']}, escalations {s['escalations']}, "
                         f"failures/timeouts {s['failures']}, quota hits {s['quota']}")
    else:
        lines.append('Workers: no activity in the last 7 days')
    if projects:
        lines.append('Projects:')
        for project in projects:
            s = _summarise(name=project, scope='project', done=done, returns=returns,
                           escalations=escalations, failures=failures, quotas=quotas)
            lines.append(f"- {project}: done {s['done']}, median cycle {s['median']}, "
                         f"returns {s['returns']}, escalations {s['escalations']}, "
                         f"failures/timeouts {s['failures']}, quota hits {s['quota']}")
    else:
        lines.append('Projects: no activity in the last 7 days')

    causes = _delay_causes(state, now_ts)
    if causes:
        lines.append('Top 3 delay causes: ' + '; '.join(f'{label} ({count})' for label, count in causes[:3]))
    else:
        lines.append('Top 3 delay causes: none recorded')

    approved = [e for e in (state.get('improvements') or [])
                if isinstance(e, dict) and (e.get('approved') or e.get('approved_at'))]
    if approved:
        lines.append('Approved improvements (before -> after, last 7 days):')
        for entry in approved[:MAX_IMPROVEMENTS]:
            after = _metric_value(state, now_ts, entry.get('metric'))
            lines.append(f"- {entry.get('title') or 'Improvement'}: "
                         f"{describe_metric(entry.get('metric'), entry.get('before'))} -> "
                         f"{describe_metric(entry.get('metric'), after)}")
    return '\n'.join(lines)


def build_improvements(state, now):
    """Up to 3 deterministic Improvement proposals, worst problem first. Pure; no LLM, no Deck calls."""
    state = state or {}
    now_ts = _now_ts(now)
    done = _recent(state.get('done_log'), now_ts)
    returns = _recent(state.get('return_log') or state.get('returned_log'), now_ts)
    escalations = _recent(state.get('escalation_log'), now_ts)
    failures = _recent(state.get('failure_log'), now_ts)
    quotas = _recent(state.get('quota_log'), now_ts)

    scored = []
    projects = sorted({_project_of(e) for e in done + returns + escalations + failures if _project_of(e)})
    for project in projects:
        mine_returns = [e for e in returns if _project_of(e) == project]
        mine_failures = [e for e in failures if _project_of(e) == project]
        mine_done = [e for e in done if _project_of(e) == project]
        if mine_returns:
            n = len(mine_returns)
            scored.append((n * 10, f'Reduce returns in {project}',
                           f'{n} card(s) returned or stalled in {project} in the last 7 days.',
                           f'{n} returned/stalled cards (7 days).',
                           'Tighten the Definition of Ready and split the most-returned cards before they enter To do.',
                           f'returns:project:{project}',
                           'Compare returns next Monday with this week.'))
        median = _median_cycle(mine_done)
        if median is not None and median > DAY:
            scored.append((int(median / DAY), f'Shorten cycle time in {project}',
                           f'Median cycle time in {project} is {_duration(median)}.',
                           f'{_duration(median)} median over {len(mine_done)} completed cards (7 days).',
                           'Cap WIP lower and make acceptance tests explicit so review rounds are shorter.',
                           f'cycle:project:{project}',
                           'Compare the median cycle time next Monday with this week.'))
        if len(mine_failures) >= 2:
            n = len(mine_failures)
            scored.append((n, f'Cut failures and timeouts in {project}',
                           f'{n} worker failures/timeouts in {project} in the last 7 days.',
                           f'{n} failures/timeouts (7 days).',
                           'Add the failing command to the card and run it before the worker hand-off.',
                           f'failures:project:{project}',
                           'Compare failures next Monday with this week.'))
    for worker in sorted({_worker_of(e) for e in quotas if _worker_of(e)}):
        n = len([e for e in quotas if _worker_of(e) == worker])
        scored.append((n * 10, f'Reduce quota-limit stalls for {worker.capitalize()}',
                       f'{worker.capitalize()} hit a quota or limit {n} time(s) in the last 7 days.',
                       f'{n} quota/limit hits (7 days).',
                       'Move that worker to a project that fits its quota window and keep one card in flight.',
                       f'quota:worker:{worker}',
                       'Compare quota hits next Monday with this week.'))
    for label, count in _delay_causes(state, now_ts):
        if label in ('stalled/no heartbeat', 'tests/CI failing', 'review fixes', 'unready spec') and count >= 2:
            project = ''
            for candidate in projects:
                if any(_cause_label(e.get('reason') or e.get('note')) == label
                       for e in returns + failures if _project_of(e) == candidate):
                    project = candidate
                    break
            if project:
                scored.append((count, f'Remove the {label} delay cause in {project}',
                               f'{count} delays in {project} came from {label}.',
                               f'{count} occurrences (7 days).',
                               'Make the missing step part of the card checklist before the next pull.',
                               f'stalled:project:{project}' if label == 'stalled/no heartbeat'
                               else f'returns:project:{project}',
                               'Compare this cause count next Monday with this week.'))

    scored.sort(key=lambda row: (-row[0], row[1]))
    out, seen = [], set()
    for _severity, title, problem, evidence, change, metric, measure in scored:
        if title in seen:
            continue
        seen.add(title)
        out.append({'title': f'Improvement: {title}',
                    'description': (f'Problem: {problem}\n'
                                    f'Evidence (last 7 days): {evidence}\n'
                                    f'Proposed rule/config change: {change}\n'
                                    f'How we will measure it next week: {measure}'),
                    'metric': metric,
                    'before': _metric_value(state, now_ts, metric)})
        if len(out) >= MAX_IMPROVEMENTS:
            break
    return out
