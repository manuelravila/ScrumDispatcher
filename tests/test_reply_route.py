"""Untagged Talk replies reach the author of the replied-to message (Manuel 2026-10-31).
Run: /opt/company/.venv/bin/python -m unittest discover -s /opt/company/tests -v"""
import sys, unittest
from unittest import mock
sys.path.insert(0, '/opt/company')
from isolate import d


def msg(mid, actor, text, name=None, parent=None):
    m = {'id': mid, 'actorId': actor, 'actorType': 'users', 'actorDisplayName': name or actor,
         'messageType': 'comment', 'message': text, 'messageParameters': {}}
    if parent:
        m['parent'] = parent
    return m


class ReplyRoute(unittest.TestCase):
    def setUp(self):
        d.TASKS.clear(); d.TASK_OF.clear()
        d.STATE['depth'].clear(); d.STATE.pop('answers', None)
        self.room = d.CFG['projects']['hq']['room']
        d.STATE['last_id'][self.room] = 100

    def poll(self, *data):
        resp = mock.Mock(status_code=200); resp.json.return_value = {'ocs': {'data': list(data)}}
        none = mock.Mock(status_code=304)
        sess = mock.Mock(); sess.get.side_effect = lambda url, **k: resp if self.room in url else none
        jobs = []
        with mock.patch.dict(d.SESS, {'oscar': sess}), mock.patch.object(d, 'save_state'), \
                mock.patch.object(d.POOL, 'submit', side_effect=lambda *a: jobs.append(a)):
            d.poll_talk()
        return {(a[1], a[4]['id']): a[7] for a in jobs}   # (agent, mid) -> ack

    def test_ringo_answer_reaches_oscar_once(self):
        ask = msg(101, 'oscar', '@ringo Stuck step on the LinkedIn post')       # Oscar posted it himself
        d.STATE['depth']['102'] = 2; d.STATE['answers'] = {'102': 'reply'}
        ans = msg(102, 'claude', 'Oscar: I checked the composer myself. Restage.', 'Ringo', parent=ask)
        self.assertEqual(self.poll(ask, ans), {('ringo', 101): False, ('oscar', 102): True})
        self.assertEqual(d.TASKS[101]['active'], 1)   # the delivery is not a task step: no summary

    def test_asker_ack_does_not_bounce(self):
        d.STATE['depth'].update({'102': 2, '103': 3}); d.STATE['answers'] = {'102': 'reply', '103': 'ack'}
        ans = msg(102, 'claude', 'Restage.', 'Ringo')
        ack = msg(103, 'oscar', 'Restaged, thanks.', 'Oscar', parent=ans)
        self.assertEqual(self.poll(ack), {})
        ask = msg(104, 'oscar', 'Restaged. Still stuck, retry tomorrow?', 'Oscar', parent=ans)
        d.STATE['depth']['104'] = 3; d.STATE['answers']['104'] = 'ack'
        self.assertEqual(self.poll(ask), {('ringo', 104): False})   # a question goes back as a new ask

    def test_human_reply_reaches_agent(self):
        d.STATE['depth']['102'] = 1; d.STATE['answers'] = {'102': 'reply'}
        ans = msg(102, 'claude', 'Draft reviewed.', 'Ringo')
        self.assertEqual(self.poll(msg(103, 'mrar1995', 'ok, apply it', parent=ans)), {('ringo', 103): False})
        self.assertIn(103, d.TASKS)                   # Manuel's reply is a new ask

    def test_explicit_mention_wins(self):
        ans = msg(102, 'claude', 'Draft reviewed.', 'Ringo')
        self.assertEqual(self.poll(msg(103, 'mrar1995', '@freddy apply it', parent=ans)), {('freddy', 103): False})

    def test_no_route_for_relay_notes_muse_or_ringo(self):
        relay = msg(101, 'oscar', d.manuel_ask.RELAY_PREFIX + ' @ringo check v5')
        d.STATE['depth'].update({'102': 1, '104': 99, '106': 1}); d.STATE['answers'] = {'102': 'reply', '106': 'reply'}
        note = msg(104, 'oscar', 'Ringo is at the Claude session limit; I will rerun this message.')
        muse = msg(105, 'muse', 'Here is the caption.', 'Muse')
        ring = msg(106, 'claude', '@freddy build X', 'Ringo')
        got = self.poll(msg(102, 'claude', 'v5 reviewed.', 'Ringo', parent=relay),     # answer to Manuel's relay
                        msg(107, 'mrar1995', 'why?', parent=note),                     # reply to a dispatcher note
                        msg(108, 'oscar', 'Nice caption.', 'Oscar', parent=muse),       # agents never auto-reach Muse
                        msg(109, 'freddy', 'Built X, tests pass.', 'Freddy', parent=ring))  # Ringo: review loop / '?' only
        self.assertEqual(got, {})

    def test_human_reply_to_muse_reaches_muse(self):
        muse = msg(105, 'muse', 'Here is the caption.', 'Muse')
        self.assertEqual(self.poll(msg(106, 'mrar1995', 'shorter please', parent=muse)), {('muse', 106): False})


if __name__ == '__main__':
    unittest.main()
