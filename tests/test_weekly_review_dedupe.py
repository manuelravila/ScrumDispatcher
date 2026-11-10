"""Weekly pipeline review dedupe (2026-11-09: a test run created 33 duplicate cards on board 29).

Hermetic: fetch_stacks and deck are patched, and tests/isolate.py refuses every unmocked
network write, so these tests never reach a production Nextcloud.
"""
import copy
import datetime as dt
import unittest
from unittest import mock

from isolate import d


NOW = dt.datetime(2026, 11, 9, 8, 30)      # Monday 08:30 local: weekly_review.due() is true
WEEK = '2026-W46'
EMPTY_STACKS = [{'id': 137, 'title': 'To spec', 'cards': []}]


def weekly_card(cid=900, description=''):
    return {'id': cid, 'title': d.weekly_review.WEEKLY_TITLE, 'description': description,
            'labels': [], 'assignedUsers': []}


class DeckRecorder:
    """Records deck() calls; optionally fails the card POST like a network error would."""

    def __init__(self, fail_post=False):
        self.calls = []
        self.fail_post = fail_post

    def __call__(self, agent, method, path, **kw):
        self.calls.append((agent, method, path, kw))
        if self.fail_post and method == 'POST':
            raise RuntimeError('deck POST failed')
        return {}

    @property
    def posts(self):
        return [c for c in self.calls if c[1] == 'POST']


class WeeklyReviewDedupeTest(unittest.TestCase):
    def setUp(self):
        self.saved = copy.deepcopy(d.STATE)
        d.STATE.pop('weekly_review_last_week', None)
        d.STATE.pop('improvement_last_week', None)

    def tearDown(self):
        d.STATE.clear(); d.STATE.update(self.saved)

    def run_check(self, stacks, recorder, now=NOW):
        with mock.patch.object(d, 'fetch_stacks', return_value=stacks), \
             mock.patch.object(d, 'deck', recorder), \
             mock.patch.dict(d.CFG, {'specialists': True}):
            return d.check_weekly_review({}, now=now)

    def test_one_card_per_week(self):
        recorder = DeckRecorder()
        self.run_check(EMPTY_STACKS, recorder)
        self.run_check(EMPTY_STACKS, recorder)          # same poll later / next poll
        self.assertEqual(len(recorder.posts), 1)
        self.assertEqual(d.STATE['weekly_review_last_week'], WEEK)

    def test_done_card_for_the_week_blocks_creation(self):
        # due() would say true: it only counts OPEN cards. The Done card still proves the week is done.
        card = weekly_card(description=f'{d.weekly_review.WEEKLY_TITLE} for {WEEK} (created Monday).')
        stacks = [{'id': 137, 'title': 'To spec', 'cards': []},
                  {'id': 3, 'title': 'Done', 'cards': [card]}]
        self.assertTrue(d.weekly_review.due(NOW, [dict(card, stackTitle='Done')]))
        recorder = DeckRecorder()
        self.run_check(stacks, recorder)
        self.assertEqual(recorder.posts, [])
        self.assertEqual(d.STATE['weekly_review_last_week'], WEEK)

    def test_failed_post_leaves_no_marker_and_the_next_poll_retries(self):
        failing = DeckRecorder(fail_post=True)
        with self.assertRaises(RuntimeError):
            self.run_check(EMPTY_STACKS, failing)
        self.assertIsNone(d.STATE.get('weekly_review_last_week'))

        retry = DeckRecorder()
        self.run_check(EMPTY_STACKS, retry)
        self.assertEqual(len(retry.posts), 1)
        self.assertEqual(d.STATE['weekly_review_last_week'], WEEK)

    def test_isolate_blocks_live_deck_writes(self):
        with self.assertRaisesRegex(RuntimeError, 'live write attempted in tests: POST'):
            d.deck('ringo', 'POST', '/boards/29/stacks/1/cards', json={})


if __name__ == '__main__':
    unittest.main()
