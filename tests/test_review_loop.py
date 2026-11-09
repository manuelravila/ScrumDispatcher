"""review_loop must survive a fix round that comes back from a non-roster poster ('oscar').
Regression for KeyError: 'oscar' (2026-10-16 07:04, docreview card, Freddy paused/busy).
Run: /opt/company/.venv/bin/python -m unittest discover -s /opt/company/tests -v"""
import sys, unittest
from unittest import mock
sys.path.insert(0, '/opt/company')
from isolate import d


class ReviewLoopNonRoster(unittest.TestCase):
    def run_loop(self, fix_reply):
        calls = []
        def fake_execute(agent, project, task, context, notify, thread_key=None):
            calls.append(agent)
            if agent == 'ringo':
                return 'ringo', 'FIXES\n1. fix the thing', None
            return fix_reply
        notes = []
        with mock.patch.object(d, 'execute', side_effect=fake_execute):
            out = d.review_loop('docreview', 'task', 'freddy', 'report', 'card-1', notes.append)
        return out, calls, notes

    def test_requeue_from_oscar(self):
        (verdict, body, coder, _), calls, _ = self.run_loop(('oscar', 'Freddy paused', 'REQUEUE'))
        self.assertEqual(verdict, 'REQUEUE')
        self.assertEqual(coder, 'freddy')
        self.assertIn('fix the thing', body)
        self.assertEqual(calls, ['ringo', 'freddy'])

    def test_oscar_without_flag(self):
        # e.g. "Freddy's task was cancelled by ..." returns ('oscar', text, None)
        (verdict, _, coder, _), _, _ = self.run_loop(('oscar', 'cancelled', None))
        self.assertEqual(verdict, 'REQUEUE')
        self.assertEqual(coder, 'freddy')

    def test_display_handles_any_name(self):
        self.assertEqual(d.display('oscar'), 'Oscar')
        self.assertEqual(d.display('freddy'), d.CFG['agents']['freddy']['display'])

    def test_review_prompt_with_oscar_as_coder(self):
        # Oscar itself as the coder/poster must not crash the prompt build
        with mock.patch.object(d, 'execute', return_value=('ringo', 'APPROVED', None)):
            verdict, *_ = d.review_loop('docreview', 't', 'oscar', 'r', 'k', lambda n: None)
        self.assertEqual(verdict, 'APPROVED')


if __name__ == '__main__':
    unittest.main()
