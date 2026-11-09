"""One Talk completion message per finished task (Manuel 2026-10-16).
Run: /opt/company/.venv/bin/python -m unittest discover -s /opt/company/tests -v"""
import sys, time, unittest
from unittest import mock
sys.path.insert(0, '/opt/company')
from isolate import d


def msg(mid, actor, text, name='Manuel Avila'):
    return {'id': mid, 'actorId': actor, 'actorType': 'users', 'actorDisplayName': name,
            'messageType': 'comment', 'message': text, 'messageParameters': {}}


class TaskReports(unittest.TestCase):
    def setUp(self):
        d.TASKS.clear(); d.TASK_OF.clear()
        self.posts = []
        self.p_post = mock.patch.object(d, 'talk_post', side_effect=lambda *a, **k: self.posts.append((a, k)) or 900 + len(self.posts))
        self.p_sum = mock.patch.object(d, 'summarize', return_value='- did it\nPending: nothing')
        self.p_pool = mock.patch.object(d.POOL, 'submit', side_effect=lambda fn, *a: fn(*a))
        for p in (self.p_post, self.p_sum, self.p_pool):
            p.start(); self.addCleanup(p.stop)

    def run_job(self, root, who, text, posted):
        d.task_job_start(root)
        d.task_note(root, who, text, posted)
        d.task_job_end(root)

    def settle(self):
        for t in d.TASKS.values():
            if t['idle_since']:
                t['idle_since'] -= d.TASK_QUIET + 1
        d.check_tasks()

    def test_single_reply_posts_nothing_extra(self):
        d.task_open(1, 'hq', 'room', msg(1, 'mrar1995', '@ringo do X'))
        self.run_job(1, 'ringo', 'done\nPending: nothing', 2)
        self.settle()
        self.assertEqual(self.posts, [])
        self.assertEqual(d.TASKS, {})

    def test_chain_posts_one_summary_after_last_job(self):
        d.task_open(1, 'hq', 'room', msg(1, 'mrar1995', '@ringo build X'))
        self.run_job(1, 'ringo', '@freddy please build X', 2)
        self.assertEqual(d.TASK_OF[2], 1)          # Freddy's job will inherit the root
        self.run_job(d.TASK_OF[2], 'freddy', 'built X', 3)
        d.task_job_start(1)                         # a third job is running: not finished yet
        self.settle()
        self.assertEqual(self.posts, [])
        d.task_note(1, 'ringo', 'APPROVED', 4); d.task_job_end(1)
        self.settle(); self.settle()
        self.assertEqual(len(self.posts), 1)
        (agent, room, text), kw = self.posts[0]
        self.assertEqual((agent, room, kw['reply_to'], kw['depth']), ('oscar', 'room', 1, 99))
        self.assertIn('Task finished: "build X"', text)
        self.assertIn('Pending:', text)

    def test_not_finished_before_quiet_period(self):
        d.task_open(1, 'hq', 'room', msg(1, 'mrar1995', '@ringo X'))
        self.run_job(1, 'ringo', '@freddy go', 2)
        self.run_job(1, 'freddy', 'done', 3)
        d.check_tasks()                             # within TASK_QUIET: the poll may still spawn a job
        self.assertIn(1, d.TASKS)

    def test_poll_opens_tasks_only_for_requesters_new_asks(self):
        room = d.CFG['projects']['hq']['room']
        d.STATE['last_id'][room] = 10
        d.STATE['depth'].pop('12', None); d.STATE['depth']['13'] = 1
        data = [msg(11, 'mrar1995', '@ringo X'),
                msg(12, 'paula', '@ringo Y', 'Paula'),            # not a requester
                msg(13, 'oscar', '@freddy Z', 'Oscar')]           # dispatcher-posted reply, not a new ask
        resp = mock.Mock(status_code=200); resp.json.return_value = {'ocs': {'data': data}}
        none = mock.Mock(status_code=304)
        jobs = []
        sess = mock.Mock(); sess.get.side_effect = lambda url, **k: resp if room in url else none
        with mock.patch.dict(d.SESS, {'oscar': sess}), mock.patch.object(d, 'save_state'), \
                mock.patch.object(d.POOL, 'submit', side_effect=lambda *a: jobs.append(a)):
            d.poll_talk()
        self.assertEqual(set(d.TASKS), {11})
        roots = {a[4]['id']: a[6] for a in jobs}
        self.assertEqual(roots, {11: 11, 12: None, 13: None})
        self.assertEqual(d.TASKS[11]['active'], 1)

    def test_summarize_fallback_and_no_mentions(self):
        self.p_sum.stop()
        with mock.patch.object(d, 'run_cmd', return_value=(1, '', 'boom')):
            out = d.summarize('X', [('freddy', 'done, thanks @ringo')])
        self.p_sum.start()
        self.assertNotIn('@ringo', out)
        self.assertIn('Pending:', out)


if __name__ == '__main__':
    unittest.main()
