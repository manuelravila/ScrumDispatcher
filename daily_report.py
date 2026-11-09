"""Scrum Dispatcher daily report (TASK4 part B): pure data -> text, no LLM and no network.

The dispatcher owns the data work: it passes STATE and a Deck snapshot to build_report(), writes the
returned text to outputs/daily-report.md and posts it to the HQ Talk room. Oscar's Hermes cron only
forwards the file to Manuel on Telegram.

deck_snapshot accepted formats (both are plain data):
    [{'project': 'hq', 'board': 29, 'stacks': [{'title': 'To do', 'cards': [...]}, ...]}, ...]
    {'projects': [ ...same entries... ], 'workers': ['freddy', 'gem', ...]}   # workers optional

STATE keys read here (all optional):
    done_log        [{'cid','title','worker','project','done_at','cycle_seconds'}]
    return_log      [{'cid','title','worker','project','reason','at'}]  (returned_log also accepted)
    escalation_log  [{'cid','title','project','agent','at'}]
    claims          {cid: {'worker','claimed_at','last_activity'}}
    deck_active     {cid: 'agent@project'}
    agy_limit_until_<agent>, claude_limit_until
    todo_since      {cid: epoch of the card entering To do}
"""
import datetime as dt
import statistics
import time
from zoneinfo import ZoneInfo

REPORT_TITLE = 'Scrum Dispatcher daily report'
REPORT_TZ = 'America/Toronto'
DAY = 24 * 3600
WEEK = 7 * DAY
KEEP = 14 * DAY
MAX_LINES = 25

WORKERS = ('freddy', 'gem', 'caine', 'deepti', 'ringo', 'miku', 'argon')
WORKER_DISPLAY = {'freddy': 'Freddy', 'gem': 'Gem', 'caine': 'Caine', 'deepti': 'Deepti',
                  'ringo': 'Ringo', 'miku': 'Miku', 'argon': 'Argon'}
LABEL_WORKER = {v.lower(): k for k, v in WORKER_DISPLAY.items()}
NEEDS_MANUEL = ('Manuel', 'Decide', 'Action needed')
WAITING_STACKS = ('To spec', 'To do', 'To review', 'To test')
DONE_STACKS = ('Done', 'Finished')
WORKING_STACKS = ('Doing',)
WIP_DEFAULTS = {'per_worker_doing': 1, 'per_project_to_review': 3, 'per_project_to_test': 3}

# Caps keep the whole report under MAX_LINES even with a busy queue.
DONE_SHOWN = 2
DOING_SHOWN = 2
RETURNED_SHOWN = 2
WAITING_SHOWN = 6
NEEDS_SHOWN = 2


def _tz():
    return ZoneInfo(REPORT_TZ)


def _epoch(now):
    """Epoch seconds from None (now), a number, or a datetime (naive = Toronto wall clock)."""
    if now is None:
        return time.time()
    if isinstance(now, dt.datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=_tz())
        return now.timestamp()
    if isinstance(now, (int, float)):
        return float(now)
    raise TypeError(f'now must be a datetime or epoch, got {type(now).__name__}')


def _local(ts):
    return dt.datetime.fromtimestamp(ts, _tz())


def _stamp(ts):
    return _local(ts).strftime('%Y-%m-%d %H:%M')


def _short_clock(ts):
    return _local(ts).strftime('%a %d %b %H:%M')


def _age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m'
    if seconds < 2 * DAY:
        return f'{seconds / 3600:.1f}h'
    return f'{seconds / DAY:.1f}d'


def _entry_at(entry):
    for key in ('at', 'done_at', 'ts', 'time'):
        value = entry.get(key)
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


def _recent(entries, now, window):
    return [e for e in (entries or []) if 0 <= now - _entry_at(e) <= window]


def _norm_snapshot(deck_snapshot):
    """Return (project entries, worker names). Tolerates list or dict, missing keys and empty input."""
    if isinstance(deck_snapshot, dict):
        projects = deck_snapshot.get('projects') or []
        workers = deck_snapshot.get('workers') or list(WORKERS)
    elif isinstance(deck_snapshot, list):
        projects = deck_snapshot
        workers = list(WORKERS)
    else:
        projects = []
        workers = list(WORKERS)
    clean = [p for p in projects if isinstance(p, dict)]
    return clean, [w for w in workers if isinstance(w, str)]


def _stacks(entry):
    stacks = entry.get('stacks') or []
    return [s for s in stacks if isinstance(s, dict)]


def _cards(stack):
    return [c for c in (stack.get('cards') or []) if isinstance(c, dict)]


def _card_worker(card):
    """Worker name from a card's agent label, explicit worker field, then assignee."""
    for label in card.get('labels') or []:
        title = str((label or {}).get('title') or '')
        if title.lower() in LABEL_WORKER:
            return LABEL_WORKER[title.lower()]
    explicit = card.get('worker') or card.get('agent')
    if explicit:
        return str(explicit).lower()
    for user in card.get('assignedUsers') or []:
        uid = str((user.get('participant') or {}).get('uid') or '')
        if uid in LABEL_WORKER:
            return LABEL_WORKER[uid]
    return ''


def _card_title(card):
    return str(card.get('title') or '(no title)')


def _label_titles(card):
    return {str((l or {}).get('title') or '') for l in card.get('labels') or []}


def _done_entries(state, projects, now):
    """done_log first, plus recent Done cards from the snapshot that the log does not know yet."""
    found = {}
    for e in _recent(state.get('done_log'), now, DAY):
        cid = str(e.get('cid') or e.get('id') or '')
        if not cid:
            continue
        found[cid] = {'cid': cid, 'title': str(e.get('title') or '(no title)'),
                      'worker': str(e.get('worker') or e.get('agent') or ''),
                      'at': _entry_at(e), 'cycle': e.get('cycle_seconds')}
    for entry in projects:
        for stack in _stacks(entry):
            if stack.get('title') not in DONE_STACKS:
                continue
            for card in _cards(stack):
                cid = str(card.get('id') or '')
                if not cid or cid in found:
                    continue
                at = _entry_at({'at': card.get('done_at') or card.get('lastModified')})
                if not at or now - at > DAY:
                    continue
                found[cid] = {'cid': cid, 'title': _card_title(card),
                              'worker': _card_worker(card), 'at': at,
                              'cycle': card.get('cycle_seconds')}
    return sorted(found.values(), key=lambda e: e['at'], reverse=True)


def _doing_entries(state, projects, now):
    claims = state.get('claims') or {}
    active = state.get('deck_active') or {}
    rows = []
    for entry in projects:
        project = str(entry.get('project') or '?')
        for stack in _stacks(entry):
            if stack.get('title') not in WORKING_STACKS:
                continue
            for card in _cards(stack):
                cid = str(card.get('id') or '')
                claim = claims.get(cid) or {}
                worker = claim.get('worker') or _card_worker(card)
                if not worker and cid in active:
                    worker = str(active[cid]).split('@', 1)[0]
                claimed_at = claim.get('claimed_at')
                last = claim.get('last_activity')
                age = now - claimed_at if isinstance(claimed_at, (int, float)) else None
                beat = now - last if isinstance(last, (int, float)) else None
                rows.append({'cid': cid, 'title': _card_title(card), 'worker': worker,
                             'age': age, 'beat': beat})
    return rows


def _returned_entries(state, now):
    entries = state.get('return_log')
    if entries is None:
        entries = state.get('returned_log') or []
    rows = []
    for e in _recent(entries, now, DAY):
        cid = str(e.get('cid') or e.get('id') or '')
        if not cid:
            continue
        rows.append({'cid': cid, 'title': str(e.get('title') or '(no title)'),
                     'worker': str(e.get('worker') or e.get('agent') or ''),
                     'reason': str(e.get('reason') or e.get('note') or '').strip(),
                     'at': _entry_at(e)})
    return sorted(rows, key=lambda e: e['at'], reverse=True)


def _waiting_rows(projects):
    counts = {}
    order = []
    for entry in projects:
        project = str(entry.get('project') or '?')
        if project not in counts:
            counts[project] = {name: 0 for name in WAITING_STACKS}
            order.append(project)
        for stack in _stacks(entry):
            title = stack.get('title')
            if title in counts[project]:
                counts[project][title] += len(_cards(stack))
    return [(p, counts[p]) for p in order]


def _wip_limits(deck_snapshot):
    """Limits carried in the snapshot (dispatcher puts wip_limits() there); defaults otherwise."""
    raw = deck_snapshot.get('wip') if isinstance(deck_snapshot, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    limits = {}
    for key in ('per_worker_doing', 'per_project_to_review', 'per_project_to_test'):
        try:
            limits[key] = max(0, int(raw.get(key, WIP_DEFAULTS[key])))
        except (TypeError, ValueError):
            limits[key] = WIP_DEFAULTS[key]
    return limits


def _wip_line(projects, doing, limits):
    """One compact line: per project To review/To test vs limit, then per worker Doing vs limit."""
    parts = []
    for entry in projects:
        project = str(entry.get('project') or '?')
        counts = {'To review': 0, 'To test': 0}
        for stack in _stacks(entry):
            if stack.get('title') in counts:
                counts[stack['title']] += len(_cards(stack))
        parts.append(f"{project} To review {counts['To review']}/{limits['per_project_to_review']}, "
                     f"To test {counts['To test']}/{limits['per_project_to_test']}")
    per_worker = {}
    for row in doing:
        worker = row.get('worker') or ''
        if worker:
            per_worker[worker] = per_worker.get(worker, 0) + 1
    worker_text = ', '.join(f'{worker.capitalize()} {n}/{limits["per_worker_doing"]}'
                            for worker, n in sorted(per_worker.items())) or 'none'
    project_text = '; '.join(parts[:WAITING_SHOWN]) or 'none'
    return f'WIP vs limits: {project_text} | Doing: {worker_text}'


def _needs_manuel(projects):
    rows = []
    for entry in projects:
        project = str(entry.get('project') or '?')
        for stack in _stacks(entry):
            if stack.get('title') in DONE_STACKS:
                continue
            for card in _cards(stack):
                labels = _label_titles(card)
                hit = next((name for name in NEEDS_MANUEL if name in labels), '')
                if hit:
                    rows.append({'cid': str(card.get('id') or ''), 'title': _card_title(card),
                                 'label': hit, 'project': project})
    return rows


def _worker_lines(state, workers, now):
    claims = state.get('claims') or {}
    active = state.get('deck_active') or {}
    busy = {}
    for cid, claim in claims.items():
        if isinstance(claim, dict) and claim.get('worker'):
            busy.setdefault(str(claim['worker']), []).append(str(cid))
    for cid, value in active.items():
        agent = str(value).split('@', 1)[0]
        if agent in workers:
            busy.setdefault(agent, []).append(str(cid))

    limited = {}
    for key, value in state.items():
        if not key.startswith('agy_limit_until_') or not isinstance(value, (int, float)):
            continue
        if value <= now:
            continue
        agent = key[len('agy_limit_until_'):]
        if agent in workers:
            limited[agent] = float(value)
    claude_until = state.get('claude_limit_until') or 0
    if isinstance(claude_until, (int, float)) and claude_until > now:
        for agent in ('ringo', 'miku'):
            if agent in workers:
                limited[agent] = max(limited.get(agent, 0), float(claude_until))

    unavailable = set(busy) | set(limited)
    available = [w for w in workers if w not in unavailable]
    lines = []
    lines.append('- available: ' + (', '.join(w.capitalize() for w in available) if available else 'none'))
    if busy:
        items = []
        for agent in sorted(busy):
            cards = sorted(set(busy[agent]))
            items.append(agent.capitalize() + (f' (#{", #".join(cards)})' if cards else ''))
        lines.append('- busy: ' + ', '.join(items))
    if limited:
        items = [f'{agent.capitalize()} until {_short_clock(until)}'
                 for agent, until in sorted(limited.items(), key=lambda kv: kv[1])]
        lines.append('- limited: ' + ', '.join(items))
    return lines


def _metrics(state, now):
    cycles = []
    for e in _recent(state.get('done_log'), now, WEEK):
        value = e.get('cycle_seconds')
        if isinstance(value, (int, float)) and value >= 0:
            cycles.append(float(value))
    if cycles:
        median = statistics.median(cycles)
        cycle_text = f'median {_age(median)} on {len(cycles)} card' + ('' if len(cycles) == 1 else 's')
    else:
        cycle_text = 'median n/a (no completed cards)'
    returns = len(_recent(state.get('return_log') or state.get('returned_log'), now, WEEK))
    escalations = len(_recent(state.get('escalation_log'), now, WEEK))
    return f'Metrics: cycle time To do -> Done {cycle_text} (7d); returns {returns}; escalations {escalations}'


def build_report(state, deck_snapshot, now=None):
    """Return the short daily report text. Pure: reads only the arguments, never the network or an LLM."""
    state = state or {}
    now = _epoch(now)
    projects, workers = _norm_snapshot(deck_snapshot)
    limits = _wip_limits(deck_snapshot)
    lines = [f'{REPORT_TITLE} - {_stamp(now)} Toronto']

    done = _done_entries(state, projects, now)
    if done:
        shown = done[:DONE_SHOWN]
        extra = len(done) - len(shown)
        header = f'Done in the last 24h ({len(done)}' + (f'; showing {len(shown)}' if extra else '') + '):'
        lines.append(header)
        for e in shown:
            worker = f' ({e["worker"].capitalize()})' if e['worker'] else ''
            lines.append(f'- #{e["cid"]} {e["title"]}{worker}')
    else:
        lines.append('Done in the last 24h: none')

    doing = _doing_entries(state, projects, now)
    if doing:
        shown = doing[:DOING_SHOWN]
        extra = len(doing) - len(shown)
        lines.append(f'In progress (Doing) ({len(doing)}' + (f'; showing {len(shown)}' if extra else '') + '):')
        for e in shown:
            worker = e['worker'].capitalize() if e['worker'] else '?'
            detail = ''
            if e['age'] is not None:
                detail += f', {_age(e["age"])} in Doing'
            if e['beat'] is not None:
                detail += f', heartbeat {_age(e["beat"])} ago'
            lines.append(f'- #{e["cid"]} {e["title"]} ({worker}{detail})')
    else:
        lines.append('In progress (Doing): none')

    returned = _returned_entries(state, now)
    if returned:
        shown = returned[:RETURNED_SHOWN]
        extra = len(returned) - len(shown)
        lines.append(f'Returned/stalled in the last 24h ({len(returned)}'
                     + (f'; showing {len(shown)}' if extra else '') + '):')
        for e in shown:
            who = f' ({e["worker"].capitalize()})' if e['worker'] else ''
            reason = e['reason'][:80] + ('...' if len(e['reason']) > 80 else '')
            lines.append(f'- #{e["cid"]} {e["title"]}{who}' + (f': {reason}' if reason else ''))
    else:
        lines.append('Returned/stalled in the last 24h: none')

    waiting = _waiting_rows(projects)
    if waiting:
        shown = waiting[:WAITING_SHOWN]
        extra = len(waiting) - len(shown)
        lines.append('Waiting (To spec / To do / To review / To test):'
                     + (f' +{extra} more projects' if extra else ''))
        for project, counts in shown:
            lines.append('- ' + project + ': ' + ' / '.join(str(counts[s]) for s in WAITING_STACKS))
    else:
        lines.append('Waiting: no cards')

    lines.append(_wip_line(projects, doing, limits))

    lines.append('Workers:')
    lines.extend(_worker_lines(state, workers, now))

    needs = _needs_manuel(projects)
    if needs:
        shown = needs[:NEEDS_SHOWN]
        extra = len(needs) - len(shown)
        lines.append(f'Needs Manuel ({len(needs)}' + (f'; showing {len(shown)}' if extra else '') + '):')
        for e in shown:
            lines.append(f'- #{e["cid"]} {e["title"]} ({e["label"]}, {e["project"]})')
    else:
        lines.append('Needs Manuel: none')

    lines.append(_metrics(state, now))
    return '\n'.join(lines[:MAX_LINES])
