"""ensure_stack (phase 2, TASK2): create a Deck stack a board is missing — idempotent, no network of its own.

TASK2 needs "To test" on the project boards (To spec → To do → Doing → To review → To test → Done) created at
dispatcher start via the Deck API with the ringo session, oscar on boards 27/30. This module does not import
dispatcher or requests: it takes deck_call, the dispatcher's deck() with the agent already bound —
    deck_call(method, path, **kw)   e.g. functools.partial(dispatcher.deck, 'ringo')
— so it is unit-testable against a fake board.

ensure_stack returns the stack id. When a stack titled `after` exists (default: 'To review' for 'To test'), the
new stack is placed immediately after it; the stacks that were already after the anchor are renumbered so the
line order survives whichever way Deck chose to place the new one.
"""
import logging

log = logging.getLogger(__name__)

# New stack title -> the existing stack it must sit after (TASK2 board flow).
ORDER_AFTER = {'To test': 'To review'}
_BOARD = '/boards/{}'
_STACKS = _BOARD + '/stacks'
_REORDER = _BOARD + '/stacks/{}/reorder'


def _norm(title):
    return ' '.join(str(title or '').split())


def _stack_list(response):
    """Deck returns a list of stacks here; the OCS routes wrap it in ocs.data. Anything else is a failure."""
    if isinstance(response, dict):
        data = response.get('stacks') or (response.get('ocs') or {}).get('data')
        response = data
    if not isinstance(response, list):
        return None
    return [s for s in response if isinstance(s, dict) and s.get('id') is not None]


def _created_stack(response):
    if isinstance(response, dict):
        created = response.get('ocs', {}).get('data') if 'ocs' in response else response
        if isinstance(created, dict) and created.get('id') is not None:
            return created
    return None


def _find(stacks, title):
    wanted = _norm(title)
    exact = [s for s in stacks if _norm(s.get('title')) == wanted]
    return exact[0] if exact else None


def _order_of(stack):
    try:
        return int(stack.get('order'))
    except (TypeError, ValueError):
        return 0


def _positioned(stacks):
    return sorted(stacks, key=lambda s: (_order_of(s), int(s['id'])))


def _fetch_stacks(deck_call, board_id, path):
    stacks = _stack_list(deck_call('GET', path))
    if stacks is None:
        raise LookupError(f'deck board {board_id}: could not read the stack list')
    return stacks


def _place_after(deck_call, board_id, stacks, new_id, anchor):
    """Put the new stack directly after the anchor, pushing the previous followers down one place each."""
    ordered = _positioned(stacks)
    idx = next((i for i, s in enumerate(ordered) if int(s['id']) == new_id), None)
    anchor_idx = next((i for i, s in enumerate(ordered) if int(s['id']) == int(anchor['id'])), None)
    if idx is None or anchor_idx is None:
        raise LookupError(f'deck board {board_id}: stack {new_id} or the anchor is not on the board')
    if idx == anchor_idx + 1:
        return
    base = _order_of(anchor)
    tail = [s for s in ordered if int(s['id']) != new_id][anchor_idx + 1:]
    for step, stack in enumerate([next(s for s in ordered if int(s['id']) == new_id)] + tail, 1):
        target = base + step
        if _order_of(stack) == target:
            continue
        deck_call('PUT', _REORDER.format(board_id, stack['id']), json={'order': target})


def ensure_stack(deck_call, board_id, title, after=None):
    """Return the id of stack `title` on board `board_id`, creating it when missing. `after` (default from
    ORDER_AFTER) names the stack the new one must sit after; pass None to append at the end."""
    title = _norm(title)
    if not title:
        raise ValueError('stack title is empty')
    path = _STACKS.format(board_id)
    stacks = _fetch_stacks(deck_call, board_id, path)
    existing = _find(stacks, title)
    if existing is not None:
        return int(existing['id'])

    anchor_title = ORDER_AFTER.get(title) if after is None else after
    anchor = _find(stacks, anchor_title) if anchor_title else None
    if anchor is not None:
        order = _order_of(anchor) + 1
    else:
        order = max([_order_of(s) for s in stacks], default=0) + 1

    created = deck_call('POST', path, json={'title': title, 'order': order})
    log.info('deck: creating stack %r on board %s at order %s', title, board_id, order)

    new = _created_stack(created)
    after_create = _fetch_stacks(deck_call, board_id, path)
    if new is None or _find(after_create, title) is None:
        raise LookupError(f'deck board {board_id}: stack {title!r} was created but is not on the board')
    new_id = int(_find(after_create, title)['id'])
    if anchor is not None:
        _place_after(deck_call, board_id, after_create, new_id, anchor)
    return new_id
