"""Import the dispatcher for tests without touching the live state.json, dispatcher.log, manuel_ask.db or repos
(2026-10-25: a test run had rewritten state.json and logged into dispatcher.log)."""
import logging, os, sys, tempfile
sys.path.insert(0, '/opt/company')
import dispatcher as d

TMP = tempfile.mkdtemp(prefix='hqtest-')
d.STATE_FILE = os.path.join(TMP, 'state.json')
d.STATE.clear(); d.STATE.update(d.load_state())   # import read the live state.json; tests start empty
d.manuel_ask.DB = os.path.join(TMP, 'manuel_ask.db')
d.manuel_ask.MUSE_DIR = os.path.join(TMP, 'muse-decisions')
for h in list(logging.root.handlers):
    logging.root.removeHandler(h); h.close()
logging.root.addHandler(logging.NullHandler())
real_tree_snapshot = d.tree_snapshot
d.tree_snapshot = lambda repo: None
