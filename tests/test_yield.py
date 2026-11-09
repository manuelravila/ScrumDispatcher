"""A fallback card gives Freddy back to the primary project at a round boundary (2026-10-21)."""
import sys, unittest
from unittest import mock
sys.path.insert(0, '/opt/company')
from isolate import d


class YieldTest(unittest.TestCase):
    def setUp(self):
        d.FREDDY_WANTED.clear()
        # the fixture: LucidMail as a fallback behind DocReview (live company.json made it primary on 2026-10-31)
        patcher = mock.patch.dict(d.CFG['projects']['lucidmail'], fallback_for='docreview')
        patcher.start(); self.addCleanup(patcher.stop)

    def _rounds(self, project):
        with mock.patch.object(d, 'run_freddy', side_effect=d.FreddyTimeout('half done')), \
             mock.patch.object(d, 'tree_state', side_effect=lambda r: object()), \
             mock.patch.object(d, 'build_prompt', return_value='p'), \
             mock.patch.object(d.os.path, 'exists', return_value=False):
            return d.run_freddy_rounds(project, 'task', 'ctx', lambda n: None)

    def test_fallback_yields_when_primary_has_a_ready_card(self):
        d.FREDDY_WANTED.add('docreview')
        with mock.patch.dict(d.CFG['projects']['lucidmail'], fallback_min_rounds=1), self.assertRaises(d.FreddyYield) as cm:
            self._rounds('lucidmail')
        self.assertEqual(cm.exception.args[:2], ('docreview', 1))

    def test_no_yield_without_a_ready_primary_card(self):
        out = self._rounds('lucidmail')            # runs every round, then reports unfinished
        self.assertIn('rounds used', out)

    def test_primary_never_yields(self):
        d.FREDDY_WANTED.add('docreview')
        self.assertIn('rounds used', self._rounds('docreview'))

    def test_execute_turns_a_yield_into_requeue(self):
        with mock.patch.object(d, 'brain_available', return_value=(True, '')), \
             mock.patch.object(d, 'run_freddy_rounds', side_effect=d.FreddyYield('docreview', 2, 'x')), \
             mock.patch.object(d.os.path, 'exists', return_value=False):
            ran, text, flag = d.execute('freddy', 'lucidmail', 't', 'c', lambda n: None)
        self.assertEqual(flag, 'REQUEUE')
        self.assertTrue(text.startswith('Freddy paused this card after round 2'))


if __name__ == '__main__':
    unittest.main()
