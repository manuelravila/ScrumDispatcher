"""Import the dispatcher for tests without touching live state, logs, databases or Nextcloud.

The dispatcher is imported from THIS repository (the parent of tests/) and runs against a
scratch COMPANY_HOME created with mkdtemp(), so no test can read or write the live
state.json, dispatcher.log, manuel_ask.db, or a production Deck board.
(2026-10-25: a test run had rewritten state.json and logged into dispatcher.log.
 2026-11-09: an unmocked poll created 33 duplicate cards on production board 29.)

Deck/Talk sessions in d.SESS are replaced with a no-network stub: GET is served from
empty in-memory payloads, while POST/PUT/DELETE raise loudly so an unmocked production
write fails instead of being silently faked or actually executed.
"""
import json, logging, os, shutil, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix='hqtest-')

# dispatcher.py loads {HOME}/company.json at import and raises without it. The live
# company.json is git-ignored, so fall back to the example shipped in the repo.
_config_src = os.path.join(ROOT, 'company.json')
if not os.path.exists(_config_src):
    _config_src = os.path.join(ROOT, 'company.example.json')
with open(_config_src) as _f:
    _config = json.load(_f)
with open(os.path.join(TMP, 'company.json'), 'w') as _f:
    json.dump(_config, _f, indent=1)

# dispatcher.py imports manuel_ask from {HOME}/tools and silently degrades to a no-op
# stub when the file is missing. Copy the real helper when this checkout has one.
_tools_src = os.path.join(ROOT, 'tools')
if os.path.isdir(_tools_src):
    shutil.copytree(_tools_src, os.path.join(TMP, 'tools'))

sys.path.insert(0, ROOT)
_previous_home = os.environ.get('COMPANY_HOME')
os.environ['COMPANY_HOME'] = TMP
try:
    import dispatcher as d
finally:
    if _previous_home is None:
        os.environ.pop('COMPANY_HOME', None)
    else:
        os.environ['COMPANY_HOME'] = _previous_home

# Tests start from a fresh scratch state, never the state the import may have read.
d.STATE_FILE = os.path.join(TMP, 'state.json')
d.STATE.clear(); d.STATE.update(d.load_state())
d.manuel_ask.DB = os.path.join(TMP, 'manuel_ask.db')
d.manuel_ask.MUSE_DIR = os.path.join(TMP, 'muse-decisions')
os.makedirs(d.manuel_ask.MUSE_DIR, exist_ok=True)
for h in list(logging.root.handlers):
    logging.root.removeHandler(h); h.close()
logging.root.addHandler(logging.NullHandler())
real_tree_snapshot = d.tree_snapshot
d.tree_snapshot = lambda repo: None


class _FakeResponse:
    """A 200 response whose body is an empty local payload; nothing leaves the process."""

    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _NoNetworkSession:
    """Read-only stand-in for a requests.Session.

    GET returns an empty payload ([] for bare-array Deck endpoints, {'ocs': {'data': []}}
    for comments, Talk and OCS routes). Any write raises before it can reach the network.
    """

    @staticmethod
    def _payload(url):
        if '/comments' in url or '/chat/' in url or '/ocs/' in url:
            return {'ocs': {'data': []}}
        return []

    def get(self, url, **kw):
        return _FakeResponse(self._payload(url))

    def request(self, method, url, **kw):
        if method.upper() == 'GET':
            return _FakeResponse(self._payload(url))
        raise RuntimeError(f'live write attempted in tests: {method.upper()} {url}')

    def post(self, url, **kw):
        raise RuntimeError(f'live write attempted in tests: POST {url}')

    def put(self, url, **kw):
        raise RuntimeError(f'live write attempted in tests: PUT {url}')

    def delete(self, url, **kw):
        raise RuntimeError(f'live write attempted in tests: DELETE {url}')


for _name in list(d.SESS):
    d.SESS[_name] = _NoNetworkSession()
