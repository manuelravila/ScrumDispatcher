"""Claude usage limit must never become a reply or a verdict (2026-10-26: #363/#364 went to Decide on
"You've hit your session limit · resets 3:30pm (America/Toronto)").
Run: /opt/company/.venv/bin/python -m unittest discover -s /opt/company/tests -v"""
import json, sys, time, unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo
sys.path.insert(0, '/opt/company')
from isolate import d

TZ = ZoneInfo('America/Toronto')
LIMIT_EV = {'type': 'result', 'is_error': True, 'subtype': 'success', 'api_error_status': 429,
            'api_error': 'usage_limit_reached', 'terminal_reason': 'api_error', 'total_cost_usd': 0,
            'result': "You've hit your session limit · resets 3:30pm (America/Toronto)", 'session_id': 'x'}


def at(y, mo, day, h, mi=0):
    return datetime(y, mo, day, h, mi, tzinfo=TZ).timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        d.STATE.pop('claude_limit_until', None)
        d.STATE['pending_reviews'] = {}
        d.STATE['deferred_talk'] = []
        d.STATE['deck_active'] = {}
        self.posts = []
        p = mock.patch.object(d, 'talk_post', side_effect=lambda *a, **k: self.posts.append((a, k)) or 1)
        p.start(); self.addCleanup(p.stop)


class ResetParse(Base):
    def test_todays_event(self):
        now = at(2026, 10, 26, 12, 31)
        self.assertEqual(d.claude_limit_from(LIMIT_EV, now), at(2026, 10, 26, 15, 30) + d.LIMIT_MARGIN)

    def test_next_day(self):
        now = at(2026, 10, 26, 22, 0)
        self.assertEqual(d.limit_reset('resets 9am (America/Toronto)', now), at(2026, 10, 27, 9) + d.LIMIT_MARGIN)

    def test_dated(self):
        now = at(2026, 10, 26, 12, 0)
        self.assertEqual(d.limit_reset("You've hit your weekly limit · resets Oct 28, 9am (America/Toronto)", now),
                         at(2026, 10, 28, 9) + d.LIMIT_MARGIN)

    def test_garbage_and_non_limit(self):
        now = 1_800_000_000
        self.assertEqual(d.limit_reset('limit reached, try later', now), now + 3600 + d.LIMIT_MARGIN)
        self.assertIsNone(d.claude_limit_from({'type': 'result', 'is_error': False, 'result': 'APPROVED'}))
        self.assertIsNone(d.claude_limit_from({'type': 'result', 'is_error': True, 'subtype': 'error_max_turns',
                                               'result': ''}))


def fake_run(ev):
    calls = []
    def run_cmd(cmd, cwd, timeout, stdin=None, live_log=None, agent=None):
        calls.append(cmd)
        return 0, json.dumps(ev) + '\n', ''
    return run_cmd, calls


class RunClaude(Base):
    def test_limit_raises_and_gates(self):
        rc, calls = fake_run(LIMIT_EV)
        with mock.patch.object(d, 'run_cmd', side_effect=rc), \
                mock.patch.object(d, 'thread_session', return_value={'sid': 's', 'ts': time.time(), 'turns': 1}):
            with self.assertRaises(d.ClaudeLimit):
                d.run_claude('ringo', 'hq', 'p', 'card-1')
            self.assertEqual(len(calls), 1)            # no "retry fresh" on a limit
            self.assertTrue(d.claude_limited())
            with self.assertRaises(d.ClaudeLimit):     # gate closed: no new run
                d.run_claude('miku', 'hq', 'p')
            self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.posts), 1)           # one hq note per window
        self.assertNotIn('@', self.posts[0][0][2])

    def test_other_error_is_failed_not_reply(self):
        rc, _ = fake_run({'type': 'result', 'is_error': True, 'subtype': 'error_during_execution', 'result': 'boom'})
        with mock.patch.object(d, 'run_cmd', side_effect=rc):
            ran, text, flag = d.execute('ringo', 'hq', 't', 'c', lambda n: None)
        self.assertEqual(flag, 'FAILED')
        rc, _ = fake_run({'type': 'result', 'is_error': True, 'subtype': 'error_during_execution', 'result': 'boom'})
        with mock.patch.object(d, 'run_cmd', side_effect=rc):
            verdict, *_ = d.review_loop('docreview', 't', 'freddy', 'r', 'card-9', lambda n: None)
        self.assertEqual(verdict, 'FAILED')


class DeckCards(Base):
    CARD = {'id': 363, 'title': 'T', 'stackId': 1, 'labels': []}
    STACKS = {'To do': 1, 'Doing': 2}

    def common(self):
        self.deck_calls, self.comments, self.asks = [], [], []
        ps = [mock.patch.object(d, 'deck', side_effect=self.fake_deck),
              mock.patch.object(d, 'deck_comment', side_effect=lambda a, c, t: self.comments.append((a, t))),
              mock.patch.object(d, 'ask_manuel', side_effect=lambda *a, **k: self.asks.append(a)),
              mock.patch.object(d, 'mark_decide', side_effect=lambda *a: self.asks.append(('decide',) + a)),
              mock.patch.object(d, 'summarize', return_value='sum'),
              mock.patch.object(d.SESS['oscar'], 'get', return_value=mock.Mock(status_code=500))]
        for p in ps:
            p.start(); self.addCleanup(p.stop)

    def fake_deck(self, agent, method, path, **kw):
        self.deck_calls.append((method, path, kw.get('json')))
        if path.endswith(f"/boards/{d.CFG['projects']['docreview']['boards'][0]}"):
            return {'labels': [{'title': t, 'id': i} for i, t in enumerate(['Decide', 'To review', 'Freddy'])]}
        return None

    def test_review_limit_holds_card_then_resumes(self):
        self.common()
        board = d.CFG['projects']['docreview']['boards'][0]
        d.STATE['deck_active']['363'] = 'freddy@docreview'
        ringo = mock.Mock(side_effect=d.ClaudeLimit('ringo', time.time() + 600, 'limit'))
        def execute(agent, project, task, context, notify, thread_key=None):
            return ('freddy', 'done, tests pass', None) if agent == 'freddy' else ringo()
        with mock.patch.object(d, 'execute', side_effect=execute):
            d.deck_job('freddy', 'docreview', board, self.STACKS, dict(self.CARD))
        self.assertIn('363', d.STATE['pending_reviews'])
        self.assertIn('363', d.STATE['deck_active'])                  # still blocks the project
        self.assertEqual(self.asks, [])                                # no Decide, no Telegram ask
        self.assertFalse(any('assignLabel' in p for _, p, _ in self.deck_calls))
        self.assertFalse(any('@mrar1995' in a[2] for a, _ in self.posts))
        self.assertFalse(any(j and j.get('stackId') == 1 for _, _, j in self.deck_calls[1:]))  # not back to To do
        # gate open: only the review reruns, coder is not called again
        d.STATE['claude_limit_until'] = 0
        calls = []
        def execute2(agent, *a, **k):
            calls.append(agent); return 'ringo', 'APPROVED', None
        with mock.patch.object(d, 'execute', side_effect=execute2), \
                mock.patch.object(d.POOL, 'submit', side_effect=lambda fn, *a: fn(*a)):
            d.resume_held()
        self.assertEqual(calls, ['ringo'])
        self.assertNotIn('363', d.STATE['pending_reviews'])
        self.assertNotIn('363', d.STATE['deck_active'])
        self.assertTrue(any(p.endswith('/assignLabel') and j == {'labelId': 1} for _, p, j in self.deck_calls))

    def test_ringo_card_limit_requeues(self):
        self.common()
        board = d.CFG['projects']['docreview']['boards'][0]
        with mock.patch.object(d, 'execute', side_effect=d.ClaudeLimit('ringo', time.time() + 600, 'x')):
            d.deck_job('ringo', 'docreview', board, self.STACKS, dict(self.CARD))
        self.assertEqual(self.asks, [])
        self.assertTrue(any(p.endswith('/reorder') and j == {'order': 0, 'stackId': 1} for _, p, j in self.deck_calls))
        self.assertFalse(any('assignLabel' in p for _, p, _ in self.deck_calls))
        self.assertNotIn('363', d.STATE['deck_active'])


class TalkJobs(Base):
    def test_deferred_and_resubmitted(self):
        m = {'id': 7734, 'actorDisplayName': 'Oscar', 'actorId': 'oscar', 'message': '@ringo nudge', 'messageParameters': {}}
        with mock.patch.object(d, 'talk_react'), mock.patch.object(d, 'talk_history', return_value=''), \
                mock.patch.object(d, 'execute', side_effect=d.ClaudeLimit('ringo', time.time() + 600, 'x')):
            d.talk_job('ringo', 'exmint', 'room', m, 0)
        self.assertEqual([it['m']['id'] for it in d.STATE['deferred_talk']], [7734])
        self.assertFalse(any('session limit ·' in a[2] for a, _ in self.posts))   # limit text never posted as a reply
        d.STATE['claude_limit_until'] = time.time() + 600
        subs = []
        with mock.patch.object(d.POOL, 'submit', side_effect=lambda *a: subs.append(a)):
            d.resume_held()
            self.assertEqual(subs, [])          # gate still closed
            d.STATE['claude_limit_until'] = 0
            d.resume_held()
        self.assertEqual(subs[0][0], d.talk_job)
        self.assertEqual(subs[0][4]['id'], 7734)
        self.assertEqual(d.STATE['deferred_talk'], [])


if __name__ == '__main__':
    unittest.main()
