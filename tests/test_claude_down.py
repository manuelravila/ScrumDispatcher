"""No-Claude switch (manual): the pipeline keeps running without Ringo (Manuel 2026-10-27).
With the flag file present: run_claude gates, reviews become NO_CLAUDE (card -> To review, no integration),
Ringo/Miku deck cards go to Freddy, held reviews resume instead of waiting, summaries fall back plain.
Run: /opt/company/.venv/bin/python -m unittest discover -s /opt/company/tests -v"""
import os, sys, time, unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo
sys.path.insert(0, '/opt/company')
from isolate import d, TMP

TZ = ZoneInfo('America/Toronto')


class Base(unittest.TestCase):
    def setUp(self):
        self.flag = os.path.join(TMP, 'claude-down')
        self.old_flag_path = d.CLAUDE_DOWN_FLAG
        d.CLAUDE_DOWN_FLAG = self.flag
        d.STATE.pop('claude_limit_until', None)
        d.STATE['pending_reviews'] = {}
        d.STATE['deferred_talk'] = []
        d.STATE['deck_active'] = {}
        self.posts = []
        p = mock.patch.object(d, 'talk_post', side_effect=lambda *a, **k: self.posts.append((a, k)) or 1)
        p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        d.CLAUDE_DOWN_FLAG = self.old_flag_path
        if os.path.exists(self.flag):
            os.remove(self.flag)

    def switch_on(self, reason='test'):
        with open(self.flag, 'w') as f:
            f.write(reason + '\n')


class SwitchAndReset(Base):
    def test_weekly_reset_estimate_is_thursday_15(self):
        ts = d.weekly_reset_est()
        t = datetime.fromtimestamp(ts, TZ)
        self.assertEqual(t.weekday(), 3)          # Thursday
        self.assertEqual((t.hour, t.minute), (15, 0))
        self.assertGreater(ts, time.time())
        self.assertLessEqual(ts, time.time() + 7 * 86400 + 1)

    def test_flag_toggles_live(self):
        self.assertFalse(d.claude_down())
        self.switch_on()
        self.assertTrue(d.claude_down())
        os.remove(self.flag)
        self.assertFalse(d.claude_down())


class RunClaude(Base):
    def test_manual_switch_gates_without_calling_claude(self):
        self.switch_on()
        calls = []
        with mock.patch.object(d, 'run_cmd', side_effect=lambda *a, **k: calls.append(a)):
            with self.assertRaises(d.ClaudeLimit) as ctx:
                d.run_claude('ringo', 'hq', 'p')
            with self.assertRaises(d.ClaudeLimit):
                d.run_claude('miku', 'hq', 'p')
        self.assertEqual(calls, [])                                  # never burns quota
        until = ctx.exception.args[1]
        t = datetime.fromtimestamp(until, TZ)
        self.assertEqual((t.weekday(), t.hour), (3, 15))             # reset estimate shown
        self.assertEqual(ctx.exception.args[2], 'manual no-Claude switch on')


class ReviewLoop(Base):
    def test_no_claude_verdict_without_touching_ringo(self):
        self.switch_on()
        with mock.patch.object(d, 'run_cmd', side_effect=AssertionError('claude must not be called')):
            verdict, body, coder, result = d.review_loop('docreview', 'task', 'freddy', 'report text', 'card-9',
                                                         lambda n: None)
        self.assertEqual(verdict, 'NO_CLAUDE')
        self.assertIn('No-Claude mode', body)
        self.assertIn('Ringo', body)                                  # integration deferred is stated
        self.assertEqual(coder, 'freddy')
        self.assertEqual(result, 'report text')


class DeckCards(Base):
    CARD = {'id': 363, 'title': 'T', 'stackId': 1, 'labels': [{'title': 'Freddy'}]}
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

    def test_no_claude_card_goes_to_to_review_not_held_not_decide(self):
        self.switch_on()
        self.common()
        board = d.CFG['projects']['docreview']['boards'][0]
        d.STATE['deck_active']['363'] = 'freddy@docreview'
        def execute(agent, project, task, context, notify, thread_key=None):
            return ('freddy', 'done, tests pass', None) if agent == 'freddy' else ('ringo', 'APPROVED', None)
        with mock.patch.object(d, 'execute', side_effect=execute):
            held = d.deck_job('freddy', 'docreview', board, self.STACKS, dict(self.CARD))
        self.assertFalse(held)                                        # pipeline not stopped
        self.assertEqual(d.STATE['pending_reviews'], {})              # nothing held for the reset
        self.assertNotIn('363', d.STATE['deck_active'])               # Freddy slot freed
        self.assertEqual(self.asks, [])                               # no Decide, no Telegram ask
        self.assertTrue(any(a == 'oscar' and t.startswith('NO_CLAUDE') for a, t in self.comments))
        self.assertTrue(any(p.endswith('/assignLabel') and j == {'labelId': 1} for _, p, j in self.deck_calls))
        self.assertFalse(any('@mrar1995' in a[2] for a, _ in self.posts))

    def test_held_review_resumes_as_no_claude_in_manual_mode(self):
        self.switch_on()
        self.common()
        board = d.CFG['projects']['docreview']['boards'][0]
        card = dict(self.CARD)
        d.STATE['pending_reviews']['363'] = {'agent': 'freddy', 'project': 'docreview', 'board': board,
                                             'stacks': self.STACKS, 'card': card, 'task': 't', 'ran': 'freddy',
                                             'text': 'done', 'ts': time.time()}
        d.STATE['deck_active']['363'] = 'freddy@docreview'
        def execute(agent, project, task, context, notify, thread_key=None):
            return ('freddy', 'done', None)
        with mock.patch.object(d, 'execute', side_effect=execute), \
                mock.patch.object(d.POOL, 'submit', side_effect=lambda fn, *a: fn(*a)):
            d.resume_held()
        self.assertEqual(d.STATE['pending_reviews'], {})              # unblocked, not waiting for the reset
        self.assertNotIn('363', d.STATE['deck_active'])
        self.assertTrue(any(a == 'oscar' and t.startswith('NO_CLAUDE') for a, t in self.comments))


class PollDeckReroute(Base):
    def test_relabel_swaps_ringo_label_for_freddy_once(self):
        calls, comments = [], []
        board = d.CFG['projects']['docreview']['boards'][0]

        def fake_deck(agent, method, path, **kw):
            calls.append((method, path, kw.get('json')))
            if path.endswith(f'/boards/{board}'):
                return {'labels': [{'title': 'Ringo', 'id': 7}, {'title': 'Freddy', 'id': 8}]}
            return None
        card = {'id': 500, 'title': 'T', 'stackId': 1, 'labels': [{'title': 'Ringo'}]}
        with mock.patch.object(d, 'deck', side_effect=fake_deck), \
             mock.patch.object(d, 'deck_comment', side_effect=lambda a, c, t: comments.append((a, t))):
            self.assertTrue(d.relabel_card_to_freddy(board, card, 'Ringo'))
            # already handed over: no further deck calls
            card2 = {'id': 500, 'title': 'T', 'stackId': 1, 'labels': [{'title': 'Ringo'}, {'title': 'Freddy'}]}
            self.assertTrue(d.relabel_card_to_freddy(board, card2, 'Ringo'))
        self.assertIn(('PUT', f"/boards/{board}/stacks/1/cards/500/removeLabel", {'labelId': 7}), calls)
        self.assertIn(('PUT', f"/boards/{board}/stacks/1/cards/500/assignLabel", {'labelId': 8}), calls)
        # second call: no label swap (already relabelled) and no new comment
        self.assertFalse(any(p.endswith('/removeLabel') for _, p, _ in calls[2:]))
        self.assertTrue(any(p.endswith('/assignLabel') for _, p, _ in calls[2:]))  # re-asserting the label is harmless
        self.assertEqual(len(comments), 1)
        self.assertIn('no-Claude mode', comments[0][1])


class Summarize(Base):
    def test_plain_summary_when_claude_down(self):
        self.switch_on()
        calls = []
        with mock.patch.object(d, 'run_cmd', side_effect=lambda *a, **k: calls.append(a)):
            text = d.summarize('the request', [('freddy', 'built X, tests pass')])
        self.assertEqual(calls, [])
        self.assertIn('Last update from Freddy', text)                # existing plain fallback


if __name__ == '__main__':
    unittest.main()
