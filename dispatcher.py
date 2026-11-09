#!/opt/company/.venv/bin/python
"""Scrum Dispatcher: routes Nextcloud Talk mentions and Deck cards to agents.

The user-facing name changes with the `scrum` flag (TASK4); file paths, state keys and the
company-dispatcher systemd unit deliberately keep their old names.

Agents:
  ringo  - Claude Code on Opus (tech lead). One resumable session per project.
  miku   - Claude Code on Haiku (backup coder). Fresh session per task.
  freddy - Qwen Code on Brain qwen3.8-flash-next (coder / heavy lifter). One job at a time.

Triggers:
  Talk: a message in a project room that mentions @ringo/@claude, @miku or @freddy.
  Deck: a card in "To do" on a project board labelled Ringo/Miku/Freddy, or assigned
        to that agent's Nextcloud user (claude = Ringo)
        (skipped while labelled Manuel or Decide).

Routing: Freddy first. If Brain is busy, in the nightly window, or down, the job
waits up to brain.wait_minutes and then goes to Miku. Agent replies are posted
as that agent; mentions inside replies chain to the next agent up to
max_chain_depth hops.
"""
import json, logging, os, re, shutil, signal, subprocess, sys, tempfile, threading, time, traceback, uuid
import functools
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import requests
import stacks
import muse_feed
import weekly_review
import daily_report

HOME = os.environ.get('COMPANY_HOME', '/opt/company')
sys.path.insert(0, f'{HOME}/tools')
try:
    import manuel_ask   # questions for Manuel go to Telegram (Manuel 2026-10-25, voice 2104)
except ImportError:
    import types
    manuel_ask = types.ModuleType('manuel_ask')
    manuel_ask.RELAY_PREFIX = '[Telegram]'
    manuel_ask.DB = f'{HOME}/manuel_ask.db'
    manuel_ask.MUSE_DIR = f'{HOME}/muse-decisions'
    manuel_ask.parse_options = lambda body: (body, [])
    manuel_ask.ask = lambda *a, **kw: None
LIVE_DIR = f'{HOME}/logs/live'
os.makedirs(LIVE_DIR, exist_ok=True)
CFG = json.load(open(f'{HOME}/company.json'))
STATE_FILE = f'{HOME}/state.json'
NC = CFG['nc_url'].rstrip('/')
TALK = NC + '/ocs/v2.php/apps/spreed/api/v1'
DECK = NC + '/index.php/apps/deck/api/v1.0'
DECK_OCS = NC + '/ocs/v2.php/apps/deck/api/v1.0'
TALK_MAX = 31000
DECK_COMMENT_MAX = 950

# TASK4 part A: user-facing rename only. With the `scrum` flag absent the old strings are returned, so the
# live pipeline is byte-for-byte unchanged until Ringo enables it in company.json.
SCRUM_DISPATCHER_NAME = 'Scrum Dispatcher'
LEGACY_DISPATCHER_NAME = 'Automatos HQ'
DISPATCHER_NAME = SCRUM_DISPATCHER_NAME


def dispatcher_name():
    """The display name for banners, prompts and message prefixes (gated by CFG['scrum'])."""
    return DISPATCHER_NAME if CFG.get('scrum', False) else LEGACY_DISPATCHER_NAME


def scrum_enabled():
    return bool(CFG.get('scrum', False))


DAILY_REPORT_HOUR = 9
DAILY_REPORT_TZ = 'America/Toronto'
DAILY_REPORT_PATH = f'{HOME}/outputs/daily-report.md'

logging.basicConfig(filename=f'{HOME}/logs/dispatcher.log', level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(threadName)s %(message)s')
log = logging.getLogger('hq')

# ---------------------------------------------------------------- sessions

def _env(path):
    d = {}
    try:
        f = open(path)
    except (FileNotFoundError, OSError):
        return {'NC_USER': 'mock', 'NC_PASSWORD': 'mock', 'OPENROUTER_API_KEY': 'mock', 'DEEPSEEK_API_KEY': 'mock'}
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            d[k] = v.strip().strip('"').strip("'")
    return d


def _session(user, pw):
    s = requests.Session(); s.auth = (user, pw)
    s.headers.update({'OCS-APIRequest': 'true', 'Accept': 'application/json'})
    return s


SESS = {}
for name, a in CFG['agents'].items():
    e = _env(a['env']); SESS[name] = _session(e['NC_USER'], e['NC_PASSWORD'])
W = CFG['watcher']
try:
    _token = open(W['token_file']).read().strip()
except (FileNotFoundError, OSError, KeyError):
    _token = 'mock'
SESS['oscar'] = _session(W['nc_user'], _token)
_m = _env('/root/.config/nextcloud-muse.env'); SESS['muse'] = _session(_m['NC_USER'], _m['NC_PASSWORD'])

UID2AGENT = {}
for n, a in CFG['agents'].items():
    if a.get('nc_user') and (a['nc_user'] not in UID2AGENT or n == a['nc_user']):
        UID2AGENT[a['nc_user']] = n
UID2AGENT['oscar'] = 'oscar'
UID2AGENT['muse'] = 'muse'
HANDLES = {'ringo': 'ringo', 'claude': 'ringo', 'miku': 'miku', 'freddy': 'freddy', 'oscar': 'oscar', 'vicky': 'vicky', 'muse': 'muse',
           'caine': 'caine', 'deepti': 'deepti', 'gem': 'gem', 'argon': 'argon'}
MENTION_RE = re.compile(r'@"?(ringo|claude|miku|freddy|oscar|vicky|muse|caine|deepti|gem|argon)\b', re.I)
HERMES_AGENTS = {'oscar': None, 'vicky': 'paula'}  # agent -> Hermes profile (None = default)

# ---------------------------------------------------------------- state

_lock = threading.Lock()


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except (OSError, ValueError):
        return {'last_id': {}, 'depth': {}, 'ringo_sessions': {}, 'deck_active': {}}


STATE = load_state()


def save_state():
    with _lock:
        tmp = STATE_FILE + '.tmp'
        json.dump(STATE, open(tmp, 'w'), indent=1)
        os.replace(tmp, STATE_FILE)

# ---------------------------------------------------------------- talk helpers

def talk_post(agent, room, text, reply_to=None, depth=0, answer=None):
    if len(text) > TALK_MAX:
        path = f'{HOME}/outputs/{room}-{uuid.uuid4().hex[:8]}.md'
        open(path, 'w').write(text)
        text = text[:TALK_MAX] + f'\n\n[truncated; full text on VPS: {path}]'
    data = {'message': text}
    if reply_to:
        data['replyTo'] = reply_to
    r = SESS[agent].post(f'{TALK}/chat/{room}', data=data, timeout=30)
    if r.status_code >= 300:
        log.error('talk_post %s %s %s', agent, r.status_code, r.text[:200]); return None
    mid = r.json()['ocs']['data']['id']
    with _lock:
        STATE['depth'][str(mid)] = depth
        if answer:   # an agent's answer ('reply', or 'ack' when it answers a delivered reply), not a note
            STATE.setdefault('answers', {})[str(mid)] = answer
    save_state()
    return mid


def talk_react(agent, room, mid, emoji):
    try:
        SESS[agent].post(f'{TALK}/reaction/{room}/{mid}', data={'reaction': emoji}, timeout=15)
    except requests.RequestException:
        pass


def talk_history(room, limit=15):
    r = SESS['oscar'].get(f'{TALK}/chat/{room}', params={'lookIntoFuture': 0, 'limit': limit,
                                                         'setReadMarker': 0}, timeout=30)
    if r.status_code != 200:
        return ''
    msgs = [m for m in reversed(r.json()['ocs']['data']) if m.get('messageType') == 'comment']
    return '\n'.join(f"{m['actorDisplayName']}: {render(m)[:1500]}" for m in msgs)


def render(m):
    """Replace {mention-user1} style placeholders with @uid."""
    text = m.get('message', '')
    for key, p in (m.get('messageParameters') or {}).items():
        if isinstance(p, dict):
            text = text.replace('{' + key + '}', '@' + str(p.get('id', p.get('name', ''))))
    return text


def targets_of(m):
    found = set()
    for p in (m.get('messageParameters') or {}).values():
        if isinstance(p, dict) and p.get('type') == 'user' and p.get('id') in UID2AGENT:
            found.add(UID2AGENT[p['id']])
    for h in MENTION_RE.findall(render(m)):
        found.add(HANDLES[h.lower()])
    return found


# Manuel 2026-10-31: untagged replies got lost (Ringo answered Oscar's ask without @oscar). A Talk reply now
# reaches the agent who wrote the replied-to message when it mentions nobody. Loop guard: the asker gets the
# answer once; his answer to it ('ack') goes back only with a '?'. Agents never reach Muse this way (external).
REPLY_ROUTE_SKIP = set(CFG.get('reply_route_skip', ['ringo']))   # Opus: agent replies reach him only with '?'


def reply_target(m, author):
    """Agent an untagged reply should reach, or None."""
    p = m.get('parent') or {}
    who = UID2AGENT.get(p.get('actorId')) if p.get('actorType') == 'users' else None
    if not who or who == author or render(p).startswith(manuel_ask.RELAY_PREFIX):
        return None   # human parent, own message, or Manuel's Telegram relay
    answers = STATE.get('answers', {})
    if who == 'oscar' and str(p.get('id')) in STATE['depth'] and str(p.get('id')) not in answers:
        return None   # a dispatcher note posted as Oscar, not Oscar's own words
    if author is None:
        return who    # humans: a reply always reaches the agent
    if who == 'muse' or (author == 'oscar' and str(m['id']) in STATE['depth'] and str(m['id']) not in answers):
        return None   # Muse only by @muse; dispatcher notes are not replies
    if '?' in render(m):
        return who
    if answers.get(str(m['id'])) == 'ack' or who in REPLY_ROUTE_SKIP:
        return None
    return who

# ---------------------------------------------------------------- brain gate

def in_work_hours():
    # Manuel's employer hours, server local time = America/Toronto: Mon-Fri 08:00-17:00
    t = time.localtime()
    return t.tm_wday < 5 and 8 <= t.tm_hour < 17


def brain_available():
    b = CFG['brain']
    if os.path.exists(b['nightly_flag']) or time.localtime().tm_hour in b['quiet_hours']:
        return False, 'nightly window'
    try:
        st = requests.get(b['status_url'], timeout=8).json()
        if st.get('busy') or st.get('queued'):
            return False, 'Brain busy'
        return True, ''
    except Exception:
        try:
            requests.get(b['health_url'], timeout=8).raise_for_status()
            return True, ''
        except Exception:
            return False, 'Brain unreachable'

# ---------------------------------------------------------------- prompts

ROSTER = f"""You work at {dispatcher_name()}, Manuel's home AI company.
- Manuel (@mrar1995): general manager. Final say on decisions, merges, deploys.
- Oscar: supervisor and Manuel's interface (Hermes agent).
- Ringo (@ringo): Claude Opus, tech lead. Architecture, specs, reviews. Owns quality on every project task: the dispatcher sends Freddy's and Miku's results back to him for review.
- Freddy (@freddy): Qwen Flash on Brain, coder and heavy lifter. First choice for implementation and long, token-heavy work.
- Miku (@miku): Claude Haiku. Backup coder ONLY for Oscar's and Vicky's own ad-hoc dev work (hq/paula). Never used for LucidMail or DocReview: those are low-priority and Freddy-only, so a busy or paused Freddy there means the task just waits for him instead of falling to Miku.
- Caine (@caine): Inkling (free OpenRouter tier, trains on what it sees), second coder. Only LucidMail, AutoMusic, Dashboard, HQ and ExMint (our own apps and internal tools); never DocReview, ClientA (client data) and never Manuel's work documents.
- Gem (@gem): Gemini through the Antigravity CLI (Manuel's Google AI Pro; client data allowed), coder at Freddy's level of access: any project. Gem always runs Gemini 3.8 Flash medium.
- Argon (@argon): Gemini 4 Argon on Antigravity CLI, specialist. Reviews and specs cards in To spec, writes solutions with test briefs, and solves/codes complex escalated cards.
- Deepti (@deepti): DeepSeek (paid API, no training on prompts, Chinese provider), coder. Takes Freddy's cards on ClientA and DocReview while Freddy is paused. Never Manuel's work documents.
- Paula (@paula): a family member of the operator. Vicky (@vicky, Hermes on Haiku) is her assistant and owns her room.
Rules: your final answer is posted to the team chat as you, so write it as a chat reply (concise, plain text or light markdown).
To hand work to a teammate, mention them (@freddy, @miku, @ringo, @oscar) with a clear, self-contained task. Prefer @freddy for implementation.
IMPORTANT: an @mention starts a new job for that teammate. Only @mention someone to hand over NEW work or ask a question you need answered.
Your final answer is threaded as a reply to the message that triggered you, and the dispatcher delivers it once to that message's author; their follow-up comes back only if it asks a question ('?'). So never @mention to thank, confirm, agree or report back; when it is not obvious whom you answer, start with their name (no @). To hand a reply to someone other than the author of the message you reply to, @mention them. Write names without @ otherwise.
Only reply when there is something to say: no acknowledgements, no restating what someone else already said.
When your reply finishes the task you were given, end it with one line "Pending: <what is still open and who owns it>" or "Pending: nothing".
Mention @mrar1995 when you need a decision from Manuel, in THIS room (its project's Talk room), or through Oscar for ad-hoc HQ work. Every message that mentions him reaches his Telegram, so make it self-contained (what to decide and why) and, when there are clear choices, put each on its own line as "OPTION: <short choice>" (2-4); he can tap one. Manuel's WhatsApp live feed is read-only progress updates; never expect or wait for an answer there. Never push to remote branches, deploy to production, or delete data unless the task explicitly says so.
Shared knowledge: the team wiki is the single source of truth. Before answering anything about Manuel's infrastructure, services or projects, search it with the qmd MCP tools (query, then get).
If you learn something durable that the wiki lacks, update the relevant page under /mnt/manu/files/wiki/ (start at index.md, follow SCHEMA.md) and say so in your reply.
"""

ROLE = {
    'oscar': (f'You are Oscar, supervisor and orchestrator, replying in the {dispatcher_name()} Nextcloud Talk room. Answer directly when it is quick. '
              'Never do bulk work yourself. Anything complex or that needs analysis (design, architecture, debugging, decisions, unclear scope) goes to @ringo EARLY: gather at most ~5 quick facts (paths, times, what was asked) and never investigate yourself; he specs it, assigns Freddy or Miku, and reviews. '
              'Only simple, well-defined coding or legwork goes straight to @freddy.'),
    'vicky': 'You are Vicky, Paula\'s assistant, replying in her Nextcloud Talk room. Answer directly when you can. For work that needs deep thinking (business cases, complex spreadsheet design) bring in @ringo; for long legwork bring in @freddy (Miku covers if Brain is busy). Keep Paula informed in plain language.',
    'ringo': ('You are Ringo, the tech lead. Think, decide, spec and review. Delegate implementation to @freddy with precise specs (files, acceptance criteria, how to test). '
              'You own the quality of what Freddy and Miku deliver; their results come back to you for review automatically. '
              'Do a coding, design or other complex task yourself when you judge that writing it costs fewer tokens than a spec plus correcting the coder, and say in one line why you took it. '
              'You usually start with NO memory of earlier conversations: get context from the wiki (qmd: project brief and decisions log) and the card/chat. '
              'Before you finish, write anything that must outlive this conversation (decisions, rationale, next steps) to the project wiki page or the Deck card, and say where.'),
    'caine': ('You are Caine, coder (Inkling via OpenRouter, Claude Code harness). Do the task directly, run tests or the code to verify, and report what you changed and the real results. '
              'Run long commands (full test suites, builds) in the foreground as one blocking shell call and report the exit code. Commit on the branch the task names; never push. Never rewrite history (no filter-branch/filter-repo, no rebase or reset of main or other branches, no --all ref rewrites): the repository is shared. Never commit .env* files or secrets; if one got committed on YOUR branch, stop and report it for Ringo to clean. '
              'You work in a sandbox that holds only this repository: no docker, no wiki, no other repos. If a step needs those (e.g. a docker-based test suite), say so and leave it for Ringo or Freddy. '
              'If a file or permission you need is missing or refused, NEVER stop silently: finish what you can and end with what was missing.'),
    'gem': ('You are Gem, coder (Gemini via the Antigravity CLI). Do the task directly, run tests or the code to verify, and report what you changed and the real results. '
              'Run long commands (full test suites, builds) in the foreground as one blocking shell call and report the exit code. Commit on the branch the task names; never push. Never rewrite history (no filter-branch/filter-repo, no rebase or reset of main or other branches, no --all ref rewrites): the repository is shared. Never commit .env* files or secrets. '
              'You work in a sandbox that holds only this repository: no docker, no wiki, no other repos. If a step needs those, say so and leave it for Ringo or Freddy. '
              'If a file or permission you need is missing or refused, NEVER stop silently: finish what you can and end with what was missing.'),
    'argon': ('You are Argon, specialist agent (Gemini 4 Argon on Antigravity CLI). You review cards in To spec, write solutions '
              'with approach, files, numbered acceptance test steps, priority, project, and test brief (TEST-BRIEF / END-TEST-BRIEF). '
              'You also solve and code complex escalated cards. '
              'Run long commands in the foreground and report exit codes. Do not commit secrets.'),
    'deepti': ('You are Deepti, coder (DeepSeek via Claude Code gateway), standing in for Freddy while he is paused. Do the task directly, run tests or the code to verify, and report what you changed and the real results. '
               'Run long commands (full test suites, builds) in the foreground as one blocking shell call and report the exit code. Commit on the branch the task names; never push. Never rewrite history (no filter-branch/filter-repo, no rebase or reset of main or other branches, no --all ref rewrites): the repository is shared. Never commit .env* files or secrets; if one got committed on YOUR branch, stop and report it for Ringo to clean. '
               'You work in a sandbox that holds only this repository: no docker, no wiki, no other repos. If a step needs those, or a file the task names is outside the repository, say so plainly and leave that step for Ringo or Freddy.'),
    'miku': 'You are Miku, backup coder. Do the task directly and report what you changed and how you verified it.',
    'freddy': ('You are Freddy, the coder. Do the task directly, run tests or the code to verify, and report what you changed and the real results. '
               'Long commands (builds): run them in the FOREGROUND as ONE blocking shell call that waits for the end, and set that shell call\'s timeout generously. Do not background them and poll. '
               'LucidMail test suites must NOT run on this VPS: nine containers plus a JVM GreenMail, and it has OOM-killed mysqld and systemd here. '
               'Use `/opt/company/tools/lm-test <repo-path> [ref]` instead. It transports the commit to Proxmox CT 132, runs the suite there, streams a live log, and exits with the suite\'s real exit code (0 green; 111 infrastructure; 112 gate refusal; 113 another run in progress; 124 timeout). Long runs: run the wrapper in the foreground as one blocking call with a generous timeout, or background it and poll the log path the wrapper prints. '
               'The suite now refuses to run on the VPS by itself (exit 78) unless LM_ALLOW_VPS_TEST=1; do not add that override. '
               'Never repeat the exact same tool call twice in a row: an identical repeat trips a loop guard that kills your run. '
               'If you really must wait on something, use `sleep 60; tail -n 20 <log>  # check N` with N increasing each time.'),
}


def build_prompt(agent, project, task, context):
    p = CFG['projects'][project]
    repo = p['repo'] or '(none; general HQ channel)'
    notes = f"\nRoom notes: {p['notes']}\n" if p.get('notes') else ''
    return (f"{ROSTER}\n{ROLE[agent]}\n{notes}\nProject: {project}. Repository: {repo}\n\n"
            f"Recent conversation / context:\n{context}\n\nYour task:\n{task}\n")

# ---------------------------------------------------------------- runners

def agent_git_env(agent):
    """Commits an agent makes carry its own identity, not Manuel's (Manuel 2026-10-20, Telegram 1708:
    AI agents may commit DocReview during his work hours; the history must show who did)."""
    if not agent:
        return {}
    who = f"{CFG['agents'][agent]['display']} (Automatos AI agent)"
    mail = f'{agent}@agents.automatos.ca'
    return {'GIT_AUTHOR_NAME': who, 'GIT_AUTHOR_EMAIL': mail, 'GIT_COMMITTER_NAME': who, 'GIT_COMMITTER_EMAIL': mail}


def run_cmd(cmd, cwd, timeout, stdin=None, live_log=None, agent=None):
    env = dict(os.environ, IS_SANDBOX='1', HOME='/root', CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD='1',
               PATH='/root/.local/bin:/root/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin', **agent_git_env(agent))
    if live_log:
        with open(live_log, 'w') as lf:
            p = subprocess.run(cmd, cwd=cwd, env=env, stdout=lf, stderr=subprocess.PIPE,
                               text=True, timeout=timeout, input=stdin)
        return p.returncode, open(live_log).read(), p.stderr
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, input=stdin)
    return p.returncode, p.stdout, p.stderr


def live_log_path(agent, project):
    return f"{LIVE_DIR}/{agent}_{project}_{uuid.uuid4().hex[:8]}.log"


# ---------------------------------------------------------------- pull claims / dead-worker detection
# Manuel 2026-11-09 (TASK3): a pulled card gets a claim heartbeat so the next pull tick can tell a dead
# worker from a busy one (process gone, quota/limit error, or no output/commit for stall_minutes).
# Everything here is local state: no external service is polled.
_CLAIM_CTX = threading.local()
CLAIM_HEARTBEAT_SECS = 5


def claims():
    return STATE.setdefault('claims', {})


def set_claim_context(cid):
    """Tell the runner functions which card's claim they are working on (per-thread)."""
    _CLAIM_CTX.cid = str(cid) if cid is not None else None


def current_claim_id():
    return getattr(_CLAIM_CTX, 'cid', None)


def claim_card(cid, agent, project, board):
    """Record who pulled card `cid` and when; the pull loop calls this when it hands the card over."""
    now = time.time()
    with _lock:
        claims()[str(cid)] = {'worker': agent, 'project': project, 'board': board,
                              'claimed_at': now, 'last_activity': now, 'pid': None}
    save_state()


def claim_set_pid(pid, cid=None):
    """Remember the worker process for this card (called right after Popen in the runners)."""
    cid = str(cid or current_claim_id() or '')
    if not cid:
        return
    with _lock:
        c = claims().get(cid)
        if c is None:
            return
        try:
            c['pid'] = int(pid)
        except (TypeError, ValueError):
            return
        c['last_activity'] = time.time()
    save_state()


def claim_clear_pid(cid=None):
    """The runner has reaped the worker process: a gone pid no longer means a dead job (prevents a race)."""
    cid = str(cid or current_claim_id() or '')
    if not cid:
        return
    with _lock:
        c = claims().get(cid)
        if c is None:
            return
        c['pid'] = None
    save_state()


def note_claim_activity(cid=None):
    """Refresh a claim's heartbeat (the worker emitted output or made a commit)."""
    cid = str(cid or current_claim_id() or '')
    if not cid:
        return
    with _lock:
        c = claims().get(cid)
        if c is None:
            return
        c['last_activity'] = time.time()
    save_state()


def release_claim(cid):
    with _lock:
        claims().pop(str(cid), None)
    save_state()


def pid_alive(pid):
    """True/False for a recorded pid; True when there is no usable pid (do not call it dead)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return os.path.exists(f'/proc/{pid}')


def _claim_activity_signature(agent, project, repo):
    """Fingerprint of what the worker has produced: its live log size/mtime and the repo HEAD."""
    sig = []
    try:
        logs = [p for p in os.listdir(LIVE_DIR) if p.startswith(f'{agent}_{project}_')]
        if logs:
            newest = max((os.path.join(LIVE_DIR, p) for p in logs), key=os.path.getmtime)
            st = os.stat(newest)
            sig.append((newest, st.st_size, st.st_mtime_ns))
    except OSError:
        pass
    if repo:
        try:
            r = subprocess.run(['git', '-C', repo, 'rev-parse', 'HEAD'],
                               capture_output=True, text=True, timeout=10)
            sig.append(r.stdout.strip())
        except Exception:
            pass
    return tuple(sig)


class ClaimHeartbeat(threading.Thread):
    """Refresh the claim while the worker's live log grows or its repo HEAD changes (output / commit)."""

    def __init__(self, cid, agent, project):
        super().__init__(daemon=True, name=f'claim-heartbeat-{cid}')
        self.cid, self.agent, self.project = str(cid), agent, project
        self.repo = (CFG['projects'].get(project) or {}).get('repo')
        self._stop = threading.Event()
        self._sig = None

    def run(self):
        self._sig = _claim_activity_signature(self.agent, self.project, self.repo)
        while not self._stop.wait(CLAIM_HEARTBEAT_SECS):
            try:
                sig = _claim_activity_signature(self.agent, self.project, self.repo)
                if sig != self._sig:
                    self._sig = sig
                    note_claim_activity(self.cid)
            except Exception:
                log.debug('claim heartbeat failed', exc_info=True)

    def stop(self):
        self._stop.set()


def start_claim_heartbeat(cid, agent, project):
    hb = ClaimHeartbeat(cid, agent, project)
    hb.start()
    return hb


RESUME_SECONDS = CFG.get('ringo_resume_minutes', 60) * 60
RESUME_MAX_TURNS = CFG.get('ringo_resume_max_turns', 4)


def thread_session(key):
    """Session to resume for a follow-up in the same thread/card, or None (fresh)."""
    rec = STATE.setdefault('ringo_threads', {}).get(key) if key else None
    if rec and time.time() - rec['ts'] < RESUME_SECONDS and rec['turns'] < RESUME_MAX_TURNS:
        return rec
    return None


def remember_thread(keys, rec):
    now = time.time()
    with _lock:
        th = STATE.setdefault('ringo_threads', {})
        for k in [k for k, v in th.items() if now - v['ts'] > 86400]:
            th.pop(k)
        for k in keys:
            th[k] = rec
    save_state()


# ---------------------------------------------------------------- claude usage limit
# 2026-10-26 12:31/14:00: a 429 usage_limit_reached result ("You've hit your session limit · resets 3:30pm
# (America/Toronto)") came back as Ringo's reply; parse_verdict made it NEEDS_MANUEL and #363/#364 went to Decide.
# Now a limit raises ClaudeLimit and closes one account-wide gate (Ringo, Miku and the Haiku helpers share the login)
# until the reset; held work resumes after it (pending_reviews, deferred_talk). Spec: work/ringo/hq/specs/.

class ClaudeLimit(Exception):
    """Claude usage limit hit (or the gate is still closed). args = (agent, reset_ts, text)."""


class ClaudeFailed(Exception):
    """A Claude run ended without a usable result (is_error other than a limit, or no result event)."""


LIMIT_TZ = 'America/Toronto'
LIMIT_MARGIN = 120
LIMIT_TEXT_RE = re.compile(r"hit your (?:\w+ )?limit|usage limit", re.I)
RESET_RE = re.compile(r'resets\s+(?:at\s+)?(?:([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?::(\d\d))?\s*(am|pm)'
                      r'(?:\s*\(([^)]+)\))?', re.I)
MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']


def limit_reset(text, now=None):
    """Epoch of the reset named in the limit text (+ margin); now + 1 h when it can't be parsed; at most 7 days."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    now = now or time.time()
    m = RESET_RE.search(text or '')
    ts = None
    if m:
        mon, day, h, mi, ap, tzname = m.groups()
        try:
            tz = ZoneInfo((tzname or LIMIT_TZ).strip())
        except Exception:
            tz = ZoneInfo(LIMIT_TZ)
        try:
            hour = int(h) % 12 + (12 if ap.lower() == 'pm' else 0)
            base = datetime.fromtimestamp(now, tz)
            if mon and mon[:3].lower() in MONTHS:
                t = base.replace(month=MONTHS.index(mon[:3].lower()) + 1, day=int(day), hour=hour,
                                 minute=int(mi or 0), second=0, microsecond=0)
                if t.timestamp() < now - 180 * 86400:
                    t = t.replace(year=t.year + 1)
            else:
                t = base.replace(hour=hour, minute=int(mi or 0), second=0, microsecond=0)
                if t.timestamp() <= now:
                    t += timedelta(days=1)
            ts = t.timestamp()
        except ValueError:
            ts = None
    if not ts or ts <= now:
        ts = now + 3600
    return min(ts, now + 7 * 86400) + LIMIT_MARGIN


def claude_limit_from(ev, now=None):
    """Reset epoch if this result event is a usage-limit error, else None."""
    if not ev or not ev.get('is_error'):
        return None
    text = str(ev.get('result') or '')
    err = json.dumps([ev.get('api_error'), ev.get('error')]).lower()
    if ev.get('api_error_status') == 429 or 'usage_limit' in err or 'rate_limit' in err or LIMIT_TEXT_RE.search(text):
        return limit_reset(text, now)
    return None


def claude_limited():
    """Reset epoch while the account-wide gate is closed, else 0."""
    until = STATE.get('claude_limit_until') or 0
    return until if until > time.time() else 0


def hhmm(ts):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    t = datetime.fromtimestamp(ts, ZoneInfo(LIMIT_TZ))
    return t.strftime('%H:%M') if t.date() == datetime.now(ZoneInfo(LIMIT_TZ)).date() else t.strftime('%a %d %b %H:%M')


# 2026-10-27 (Manuel, weekly-limit outage): manual no-Claude switch. When the subscription is unusable
# for days (weekly limit), HOLDING everything stalls the company. With the switch ON the pipeline keeps
# running without Ringo/Miku: Freddy takes their cards, their reviews become a NO_CLAUDE note, and
# integration to main waits for Ringo. Muse and the local agents are untouched.
CLAUDE_DOWN_FLAG = f'{HOME}/claude-down'      # touch it (any content = reason) or use the dashboard button
WEEKLY_RESET = CFG.get('claude_weekly_reset', {'weekday': 3, 'hour': 15})  # Thursday 15:00 Toronto (Manuel's observation, unverified)


def claude_down():
    """Manual no-Claude switch active (file exists). Checked live, so toggling needs no restart."""
    return os.path.exists(CLAUDE_DOWN_FLAG)


def weekly_reset_est(now=None):
    """Epoch of the next weekly Claude reset (estimate; company.json claude_weekly_reset)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    now_dt = datetime.now(ZoneInfo(LIMIT_TZ)) if now is None else datetime.fromtimestamp(now, ZoneInfo(LIMIT_TZ))
    wd, hh = WEEKLY_RESET.get('weekday', 3), WEEKLY_RESET.get('hour', 15)
    t = (now_dt + timedelta(days=(wd - now_dt.weekday()) % 7)).replace(hour=hh, minute=0, second=0, microsecond=0)
    if t.timestamp() <= now_dt.timestamp():
        t += timedelta(days=7)
    return t.timestamp()


def note_limit(agent, until, text):
    """Close the gate; one Oscar note in the hq room per limit window (no @mentions)."""
    with _lock:
        old = STATE.get('claude_limit_until') or 0
        STATE['claude_limit_until'] = max(old, until)
    save_state()
    log.warning('claude limit agent=%s until=%s text=%r', agent, hhmm(STATE['claude_limit_until']), (text or '')[:120])
    if old <= time.time():
        room = (CFG['projects'].get('hq') or {}).get('room')
        if room:
            try:
                talk_post('oscar', room, f"Claude session limit hit ({display(agent)}). Ringo/Miku jobs are held until "
                          f"{hhmm(STATE['claude_limit_until'])} and resume by themselves; held reviews keep their cards in Doing.",
                          depth=99)
            except Exception:
                log.warning('limit note failed\n%s', traceback.format_exc())


def run_claude(agent, project, prompt, thread_key=None):
    """Returns (text, session_record_or_None). Ringo starts FRESH unless thread_key points at a
    recent (< RESUME_SECONDS) Ringo session with fewer than RESUME_MAX_TURNS turns.
    Raises ClaudeLimit (usage limit / gate closed) or ClaudeFailed (no usable result): never returns those as text."""
    until = claude_limited()
    if until:
        raise ClaudeLimit(agent, until, 'gate closed')
    if claude_down():
        raise ClaudeLimit(agent, weekly_reset_est(), 'manual no-Claude switch on')
    a = CFG['agents'][agent]
    repo = CFG['projects'][project]['repo']
    if agent == 'ringo':
        cwd = f'{HOME}/work/ringo/{project}'; model = 'opus'
    else:
        cwd = f'{HOME}/work/miku'; model = 'haiku'
    os.makedirs(cwd, exist_ok=True)
    cmd = ['claude', '-p', '--model', model, '--permission-mode', 'bypassPermissions',
           '--output-format', 'stream-json', '--verbose']
    if agent == 'ringo':
        cmd += ['--effort', getattr(RINGO_EFFORT, 'value', None) or 'medium']  # Manuel 2026-10-22; low for small reviews 2026-10-30
    if repo:
        cmd += ['--add-dir', repo]
    prev = thread_session(thread_key) if agent == 'ringo' else None
    if prev:
        cmd += ['--resume', prev['sid']]
    live_log = live_log_path(agent, project)
    rc, out, err = run_cmd(cmd, cwd, a['timeout'], stdin=prompt, live_log=live_log, agent=agent)  # stdin: --add-dir is variadic and would eat a trailing prompt
    d = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get('type') == 'result':
            d = ev; break
    reset = claude_limit_from(d)
    if reset:
        note_limit(agent, reset, d.get('result'))
        raise ClaudeLimit(agent, STATE['claude_limit_until'], d.get('result') or '')
    if d is None or d.get('is_error'):
        if prev:  # stale/broken session: retry once fresh
            return run_claude(agent, project, prompt, None)
        if d is None:
            raise ClaudeFailed(f'run failed rc={rc}: {err[-800:] or out[-800:]}')
        raise ClaudeFailed(f"error result ({d.get('subtype')}, {d.get('api_error') or d.get('terminal_reason')}): "
                           f"{str(d.get('result') or '')[:800]}")
    u = d.get('usage') or {}
    tokens = sum(u.get(k) or 0 for k in ('input_tokens', 'cache_read_input_tokens',
                                         'cache_creation_input_tokens', 'output_tokens'))
    log.info('%s done project=%s %s cost=%s tokens=%s', agent, project,
             'resumed' if prev else 'fresh', d.get('total_cost_usd'), tokens)
    rec = None
    if agent == 'ringo' and d.get('session_id'):
        rec = {'sid': d['session_id'], 'ts': time.time(), 'turns': (prev['turns'] + 1) if prev else 1}
        remember_thread([thread_key] if thread_key else [], rec)
    return d.get('result') or f"(no result; subtype={d.get('subtype')})", rec


class FreddyTimeout(Exception):
    """One Freddy run hit the per-run timeout. args[0] = his output so far."""


class FreddyStopped(Exception):
    """Raised when Freddy's run was stopped on request. args[0] = who/why, args[1] = handoff (bool)."""


STOP_FLAG = f'{HOME}/stop-freddy'          # touch it (content: "cancel" = no handoff) to stop Freddy
PAUSE_FLAG = f'{HOME}/freddy-paused'       # touch/remove to reserve Brain for other clients (e.g. Manuel's work Hermes)
STOP_RE = re.compile(r'^\s*(?:@?"?freddy"?[\s,:]+(stop|cancel)|(stop|cancel)\s+@?"?freddy"?)(?:[\s,]+(?:please|now))*[\s.!]*$', re.I)
FREDDY_RUN = {'proc': None, 'stop': None}  # stop = (who, handoff)
# Projects whose To do holds a Freddy card he could start now (set by every poll_deck). A fallback
# card gives Freddy back to its primary at the next round boundary (Manuel 2026-10-21).
FREDDY_WANTED: set = set()


class FreddyYield(Exception):
    """Freddy left a fallback card between rounds because the primary project has a ready card."""


def stop_freddy(who, handoff=True):
    """Stop Freddy's running task (whole process group). Returns True if something was running."""
    proc = FREDDY_RUN['proc']
    if not proc or proc.poll() is not None:
        return False
    FREDDY_RUN['stop'] = (who, handoff)
    log.info('stopping freddy pid=%s by %s handoff=%s', proc.pid, who, handoff)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def run_freddy(project, prompt):
    repo = CFG['projects'][project]['repo'] or f'{HOME}/work/freddy'
    os.makedirs(repo, exist_ok=True)  # 2026-11-06: CT 132 move lost worker dirs
    env = dict(os.environ, HOME='/root', PATH='/root/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin',
               **agent_git_env('freddy'))
    live_log = live_log_path('freddy', project)
    lf = open(live_log, 'w')
    proc = subprocess.Popen(['/usr/local/bin/freddy', '-o', 'stream-json', prompt], cwd=repo, env=env, text=True,
                            stdout=lf, stderr=subprocess.PIPE, start_new_session=True)
    claim_set_pid(proc.pid)
    FREDDY_RUN.update(proc=proc, stop=None)
    try:
        _, err = proc.communicate(timeout=CFG['agents']['freddy']['timeout'])
        out = open(live_log).read()
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL); proc.communicate(); out = open(live_log).read()
        raise FreddyTimeout(freddy_text(out))
    finally:
        claim_clear_pid()
        FREDDY_RUN['proc'] = None
        lf.close()
    if FREDDY_RUN['stop']:
        who, handoff = FREDDY_RUN['stop']
        raise FreddyStopped(who, handoff, freddy_text(out)[-1500:])
    if proc.returncode != 0 and not out.strip():
        note_worker_limit('freddy', err)
        raise RuntimeError(f'freddy rc={proc.returncode}: {err[-500:]}')
    return freddy_text(out) or '(no output)'


def tree_state(repo):
    """Fingerprint of the working tree (HEAD + tracked diff + untracked files) to detect progress."""
    if not repo:
        return None
    g = lambda *a: subprocess.run(['git', '-C', repo, *a], capture_output=True, text=True).stdout
    untracked = g('ls-files', '-o', '--exclude-standard').split()
    stamps = []
    for f in untracked:
        try:
            st = os.stat(os.path.join(repo, f)); stamps.append(f'{f}:{st.st_size}:{st.st_mtime_ns}')
        except OSError:
            pass
    return hash((g('rev-parse', 'HEAD'), g('diff', 'HEAD'), tuple(stamps)))


def run_freddy_rounds(project, task, context, notify):
    """Freddy works in back-to-back fresh runs (like Oscar's old cron loop) so a long card is not
    cut off by one run's timeout and no single session bloats. A run that times out with progress
    in the repo is followed by a fresh one that continues from the tree. Stops when a run finishes,
    after 2 runs in a row with no repo change, or at max_rounds; that result goes to Ringo's review."""
    fc = CFG['agents']['freddy']
    repo = CFG['projects'][project]['repo']
    max_rounds = fc.get('max_rounds', 12)
    mins = fc['timeout'] // 60
    note, stale, partial = '', 0, ''
    for rnd in range(1, max_rounds + 1):
        while os.path.exists(PAUSE_FLAG):   # Manuel borrowed Brain: hold the card, resume after
            time.sleep(60)
        before = tree_state(repo)
        try:
            return run_freddy(project, build_prompt('freddy', project, task + note, context))
        except FreddyTimeout as e:
            partial = e.args[0] or ''
        if not repo:
            raise RuntimeError('freddy timed out')
        stale = 0 if tree_state(repo) != before else stale + 1
        prim = CFG['projects'][project].get('fallback_for')
        # 2026-10-25: a started fallback card keeps Freddy for fallback_min_rounds rounds (LM-37 #219 was
        # requeued 6 times after 1 round each because Ringo labelled the next DocReview card meanwhile)
        if prim and prim in FREDDY_WANTED and rnd >= CFG['projects'][project].get('fallback_min_rounds', 1):
            log.info('freddy yields %s to primary %s after round %s', project, prim, rnd)
            raise FreddyYield(prim, rnd, partial)
        if stale >= 2 or rnd == max_rounds:
            break
        log.info('freddy round %s timed out (stale=%s); continuing project=%s', rnd, stale, project)
        notify(f'Freddy hit the {mins}-min run limit (round {rnd}); starting a fresh run that continues from his edits.')
        note = (f'\n\nCONTINUATION (round {rnd + 1}): you already worked on this task in an earlier run that hit '
                f'the {mins}-min limit. Do NOT start over: run `git status` and `git diff` in {repo} and continue '
                'from those edits. While iterating run only the tests relevant to your change; run the full suite '
                'once at the end. Commit when done.'
                + (f'\nYour last output was:\n{partial[-1500:]}' if partial.strip() else ''))
    why = 'no change in the repo for 2 runs in a row' if stale >= 2 else f'{max_rounds} rounds used'
    return (f'(Freddy stopped after {rnd} run(s) of {mins} min: {why}. The work is unfinished; '
            f'see git status in {repo}.)\nLast output:\n{partial[-1500:]}')


def freddy_text(out):
    """Freddy runs qwen-code with -o stream-json (so the live feed shows progress).
    Final answer = the 'result' event; if there is none (stopped/killed), the assistant
    text blocks so far. Non-JSON output (older runs) passes through unchanged."""
    texts, saw_json = [], False
    for line in (out or '').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            texts.append(line); continue
        saw_json = True
        if ev.get('type') == 'result':
            if ev.get('is_error'):
                return f"(freddy error: {(ev.get('error') or {}).get('message') or ev.get('subtype')})"
            if ev.get('result'):
                return str(ev['result']).strip()
        elif ev.get('type') == 'assistant':
            for c in (ev.get('message') or {}).get('content') or []:
                if c.get('type') == 'text' and (c.get('text') or '').strip():
                    texts.append(c['text'].strip())
    if not saw_json:
        return (out or '').strip()
    return '\n\n'.join(texts).strip()


FREDDY_LOCK = threading.Lock()
CAINE_LOCK = threading.Lock()
GEM_LOCK = threading.Lock()
ARGON_LOCK = threading.Lock()
DEEPTI_LOCK = threading.Lock()
RINGO_LOCKS = {p: threading.Lock() for p in CFG['projects']}
MIKU_SEM = threading.Semaphore(CFG['agents']['miku'].get('max_parallel', 2))
HERMES_LOCKS = {(a, p): threading.Lock() for a in HERMES_AGENTS for p in CFG['projects']}
CODER_LOCKS = {
    'freddy': FREDDY_LOCK,
    'gem': GEM_LOCK,
    'caine': CAINE_LOCK,
    'deepti': DEEPTI_LOCK,
}
PULL_WORKERS = ('freddy', 'gem', 'caine', 'deepti')
EXCLUDED_PULL_LABELS = {
    'Manuel', 'Decide', 'Action needed', 'Later', 'Claude',
    'To review', 'Pack', 'Partner'
}


def all_agent_labels():
    labels = set()
    for name, a in CFG['agents'].items():
        if a.get('display'):
            labels.add(a['display'])
        labels.add(name)
        labels.add(name.capitalize())
        labels.add(name.title())
    labels.update({'Claude', 'Ringo', 'Miku', 'Freddy', 'Oscar', 'Vicky', 'Caine', 'Deepti', 'Gem', 'Muse'})
    return labels


AGENT_FILE_RE = re.compile(r'/opt/company/work/[\w./@+-]+')
AGENT_FILES_DIR = '.agent-files'


def stage_agent_files(repo, prompt):
    """Sandboxed coders (Caine, Deepti) see only their repo. Copy the /opt/company/work files a card names (specs,
    designs) into <repo>/.agent-files/ (git-excluded) and point the prompt there (2026-10-30: Caine's first card
    named a design file outside his sandbox and he stopped silently)."""
    staged = {}
    for path in set(AGENT_FILE_RE.findall(prompt)):
        path = path.rstrip('.,;:')
        real = os.path.realpath(path)
        if not real.startswith('/opt/company/work/') or not os.path.isfile(real) or os.path.getsize(real) > 2_000_000:
            continue
        dest = os.path.join(repo, AGENT_FILES_DIR, os.path.relpath(real, '/opt/company/work'))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(real, dest)
        staged[path] = dest
    if staged:
        excl = subprocess.run(['git', '-C', repo, 'rev-parse', '--git-path', 'info/exclude'],
                              capture_output=True, text=True).stdout.strip()
        if excl:
            excl = os.path.join(repo, excl) if not os.path.isabs(excl) else excl
            os.makedirs(os.path.dirname(excl), exist_ok=True)
            if f'/{AGENT_FILES_DIR}/' not in (open(excl).read() if os.path.exists(excl) else ''):
                open(excl, 'a').write(f'\n/{AGENT_FILES_DIR}/\n')
        for path in sorted(staged, key=len, reverse=True):
            prompt = prompt.replace(path, staged[path])
        prompt += (f'\n\nNOTE: files this task names were copied into {repo}/{AGENT_FILES_DIR}/ (read them there; '
                   'never commit that folder).\n')
    return prompt


def claude_result(out):
    """Final answer from Claude Code's stream-json event stream: the text of the 'result' event."""
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get('type') == 'result':
            return (ev.get('result') or '').strip()
    return ''


def stream_error_text(out, err=''):
    """Error text from a Claude-Code-style stream-json log: stderr plus result/error events, never the
    assistant's prose (so a card that merely mentions 'quota' cannot close a worker's gate)."""
    parts = [str(err or '')]
    for line in (out or '').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get('type') == 'result':
            if ev.get('is_error') or ev.get('api_error') or ev.get('subtype') in (
                    'error_max_turns', 'error_during_execution', 'error'):
                parts.append(str(ev.get('result') or ''))
            parts.append(json.dumps(ev.get('error') or ev.get('api_error')
                                    or ev.get('terminal_reason') or ''))
        elif ev.get('is_error'):
            parts.append(str(ev.get('result') or ''))
    return '\n'.join(parts)


def run_gem(project, prompt, agent='gem'):
    """Gem on Antigravity CLI (Manuel's Google AI Pro, Hotmail account; 2026-11-08). Same sandbox and repo as the
    Caine sandbox; the answer is the 'response' field of agy's json envelope. Raises on failure."""
    c = CFG['agents'].get(agent) or CFG['agents']['gem']
    repo = CFG['projects'][project]['repo'] or f'{HOME}/work/{agent}'
    os.makedirs(repo, exist_ok=True)
    env = dict(os.environ, HOME=c['home'], PATH=f"{c['home']}/.local/bin:/root/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin",
               IS_SANDBOX='1', **agent_git_env(agent))
    prompt = stage_agent_files(repo, prompt)
    live_log = live_log_path(agent, project)
    # 2026-11-09 (Manuel): Argon (medium) is ONLY for spec and escalations; Gem is ONLY Gemini 3.8 Flash medium.
    model = c.get('agy_model', 'gemini-4-argon-medium') if agent == 'argon' else 'gemini-3.8-flash-medium'
    with open(live_log, 'w') as lf:
        proc = subprocess.Popen(['/usr/local/bin/caine-sandbox', repo, f"{c['home']}/.local/bin/agy", '-p', prompt,
                                 '--dangerously-skip-permissions', '--output-format', 'json',
                                 '--model', model,
                                 '--print-timeout', f"{c['timeout']}s"],
                                cwd=repo, env=env, text=True, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.PIPE,
                                start_new_session=True)
        claim_set_pid(proc.pid)
        try:
            _, err = proc.communicate(timeout=c['timeout'] + 60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL); proc.communicate()
            claim_clear_pid()
            raise RuntimeError(f'agy timed out after {c["timeout"] // 60} min; work so far is in {repo}')
        claim_clear_pid()
    out = open(live_log).read()
    try:
        ev = json.loads(out)
    except ValueError:
        raise RuntimeError(f'agy rc={proc.returncode}: {(err or out)[-500:]}')
    if proc.returncode != 0 or ev.get('status') != 'SUCCESS':
        detail = str(ev.get('error') or err)
        agy_note_quota(detail, agent)
        note_worker_limit(agent, detail)
        raise RuntimeError(f'agy rc={proc.returncode} status={ev.get("status")}: {detail[-500:]}')
    return (ev.get('response') or '').strip() or '(no output)'


def agy_note_quota(text, agent='gem'):
    """2026-11-09: Gem and Argon share one Google AI Pro quota. On 'Individual quota reached ... Resets in 156h29m3s'
    close the gate for both until then, so the pull loop stops feeding cards to a dead account."""
    if 'quota reached' not in (text or '').lower():
        return
    m = re.search(r'Resets in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?', text)
    secs = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)) if m else 6 * 3600
    with _lock:
        STATE[f'agy_limit_until_{agent}'] = time.time() + max(secs, 600)
    save_state()
    _append_scrum_event('quota_log', {'agent': agent, 'project': '', 'kind': 'quota',
                                      'reason': str(text)[:300], 'at': time.time()})
    log.warning('agy quota reached: %s paused until %s', agent, hhmm(STATE[f'agy_limit_until_{agent}']))


def agy_limited(agent='gem'):
    until = STATE.get(f'agy_limit_until_{agent}') or 0
    return until if until > time.time() else 0


# Quota / auth / rate / credit trouble in a runner's output. Caine (OpenRouter) and Deepti have no agy
# reset message, so they get a default 1 h back-off while their per-agent gate is closed (TASK3).
WORKER_LIMIT_RE = re.compile(
    r'\b(?:402|429)\b|insufficient (?:credit|credits|balance|funds)|out of credits|no credits|'
    r'credit balance too low|rate.?limit|quota|unauthori[sz]ed|authentication (?:failed|error)|'
    r'invalid api key|usage limit|session limit', re.I)


def note_worker_limit(agent, text, default_secs=3600):
    """Close the agent's per-agent gate when a runner reports quota/auth/rate/credit trouble.
    Returns the back-off epoch (0 when the text is not a limit error)."""
    text = str(text or '')
    if not WORKER_LIMIT_RE.search(text):
        return 0
    m = re.search(r'[Rr]esets? in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?', text)
    secs = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)) \
        if m else default_secs
    until = time.time() + max(secs, 60)
    with _lock:
        until = max(STATE.get(f'agy_limit_until_{agent}') or 0, until)
        STATE[f'agy_limit_until_{agent}'] = until
    save_state()
    _append_scrum_event('quota_log', {'agent': agent, 'project': '', 'kind': 'limit',
                                      'reason': str(text)[:300], 'at': time.time()})
    log.warning('%s limit error: paused until %s', agent, hhmm(until))
    return until


def agent_limited(agent):
    """Back-off epoch while the agent must not pull (limit/quota/auth trouble), else 0."""
    until = STATE.get(f'agy_limit_until_{agent}') or 0
    return until if until > time.time() else 0


def run_caine(project, prompt):
    """Caine = Inkling (OpenRouter free tier) through Claude Code gateway mode (Manuel 2026-11-01 consolidation:
    OpenCode retired). OpenRouter gates the free Inkling to listed agentic harnesses: Claude Code passes (live-tested
    2026-11-01), Qwen Code gets 403. stream-json events go to the live log; answer = result event. One run at a time."""
    c = CFG['agents']['caine']
    repo = CFG['projects'][project]['repo'] or f'{HOME}/work/caine'
    os.makedirs(repo, exist_ok=True)  # 2026-11-06: CT 132 move lost worker dirs
    env = dict(os.environ, HOME=c['home'], PATH='/root/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin',
               ANTHROPIC_BASE_URL='https://openrouter.ai/api', ANTHROPIC_API_KEY='',
               ANTHROPIC_AUTH_TOKEN=_env(c['key_env'])['OPENROUTER_API_KEY'],
               ANTHROPIC_MODEL=c['model'], IS_SANDBOX='1', **agent_git_env('caine'))
    prompt = stage_agent_files(repo, prompt)
    live_log = live_log_path('caine', project)
    with open(live_log, 'w') as lf:
        proc = subprocess.Popen(['/usr/local/bin/caine-sandbox', repo,   # bwrap: Caine sees only his repo
                                 c['bin'], '-p', '--permission-mode', 'bypassPermissions',
                                 '--output-format', 'stream-json', '--verbose', prompt],
                                cwd=repo, env=env, text=True, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.PIPE,
                                start_new_session=True)   # prompt is the positional arg after the flags; stdin stays closed
        claim_set_pid(proc.pid)
        try:
            _, err = proc.communicate(timeout=c['timeout'])
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL); _, err = proc.communicate()
            claim_clear_pid()
            return claude_result(open(live_log).read()) + f"\n\n(Caine timed out after {c['timeout'] // 60} min; work so far is in {repo}.)"
        claim_clear_pid()
    out = open(live_log).read()
    text = claude_result(out)
    # OpenRouter answers 402/429/credit/auth errors inside the stream; close Caine's gate for 1 h and fail
    # the run so the stalled-worker flow returns the card instead of asking Manuel (TASK3).
    if note_worker_limit('caine', stream_error_text(out, err)):
        raise RuntimeError(f'Caine hit an OpenRouter limit/auth/credit error: '
                           f'{(text or err or out)[-500:]}')
    if proc.returncode != 0 and not text:
        raise RuntimeError(f'caine rc={proc.returncode}: {(err or out)[-500:]}')
    return text or '(no output)'


def caine_text(out):
    """Kept for any other call site: Caine's answer is the Claude Code result event now."""
    return claude_result(out)


def deepti_seconds_left(now=None):
    """Seconds until DeepSeek's next peak window, 0 inside one: Deepti runs only off-peak (Manuel 2026-10-30, voices
    2544/2546). Peak per api-docs.deepseek.com pricing: Mon-Fri 01:00-04:00 and 06:00-10:00 UTC; weekends off-peak.
    Toronto EDT = 21:00-24:00 Sun-Thu and 02:00-06:00 Mon-Fri (one hour earlier on EST). Chinese holidays (also
    off-peak in full) come from peak_utc.holidays as Beijing dates; peak hours are 09:00-18:00 Beijing, so the UTC date
    of a peak window is its Beijing date (Manuel 2026-10-31, msg 2557)."""
    pk = CFG['agents']['deepti']['peak_utc']
    holidays = set(pk.get('holidays', []))
    now = now if now is not None else time.time()
    day0 = now - now % 86400
    for d in range(8):   # peak windows from today on; the first one not yet over decides
        wd = time.gmtime(day0 + d * 86400).tm_wday
        if wd not in pk['weekdays'] or time.strftime('%Y-%m-%d', time.gmtime(day0 + d * 86400)) in holidays:
            continue
        for start, end in pk['hours']:
            s0, e0 = day0 + d * 86400 + start * 3600, day0 + d * 86400 + end * 3600
            if now < s0:
                return int(s0 - now)
            if now < e0:
                return 0
    return 7 * 86400


def deepti_for(project):
    """Deepti stands in for Freddy while he is paused, on her projects only (Manuel 2026-10-30, voice 2532)."""
    return 'deepti' in CFG['agents'] and project in CFG['agents']['deepti'].get('projects', []) \
        and os.path.exists(PAUSE_FLAG) and deepti_seconds_left() > 0


def run_deepti(project, prompt):
    """Deepti = DeepSeek (paid API) through Claude Code gateway mode at DeepSeek's official Anthropic-compatible
    endpoint (Manuel 2026-11-01 consolidation; live-tested. DSH retired as harness; the official env recipe from
    api-docs.deepseek.com is used verbatim). bwrap sandbox like Caine's (sees only the repo); stream-json events to
    the live log, answer = result event."""
    c = CFG['agents']['deepti']
    repo = CFG['projects'][project]['repo'] or f'{HOME}/work/deepti'
    os.makedirs(repo, exist_ok=True)  # 2026-11-06: CT 132 move lost worker dirs
    env = dict(os.environ, HOME=c['home'], PATH='/usr/local/bin:/usr/bin:/bin', IS_SANDBOX='1', ANTHROPIC_API_KEY='',
               ANTHROPIC_BASE_URL='https://api.deepseek.com/anthropic',
               ANTHROPIC_AUTH_TOKEN=_env(c['key_env'])['DEEPSEEK_API_KEY'],
               ANTHROPIC_MODEL='deepseek-flash[1m]', ANTHROPIC_DEFAULT_OPUS_MODEL='deepseek-flash[1m]',
               ANTHROPIC_DEFAULT_SONNET_MODEL='deepseek-flash[1m]', ANTHROPIC_DEFAULT_HAIKU_MODEL='deepseek-flash',
               CLAUDE_CODE_SUBAGENT_MODEL='deepseek-flash', CLAUDE_CODE_AUTO_COMPACT_WINDOW='786432',
               **agent_git_env('deepti'))
    prompt = stage_agent_files(repo, prompt)
    timeout = min(c['timeout'], deepti_seconds_left())   # a run never runs into DeepSeek's peak hours
    live_log = live_log_path('deepti', project)
    with open(live_log, 'w') as lf:   # stream-json events (tools, thinking) stream here for the live feed
        proc = subprocess.Popen(['/usr/local/bin/deepti-sandbox', repo, c['bin'], '-p', '--permission-mode', 'bypassPermissions',
                                 '--output-format', 'stream-json', '--verbose', prompt],
                                cwd=repo, env=env, text=True, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.PIPE,
                                start_new_session=True)
        claim_set_pid(proc.pid)
        try:
            _, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL); _, err = proc.communicate()
            claim_clear_pid()
            return claude_result(open(live_log).read()) + f"\n\n(Deepti stopped after {timeout // 60} min (time limit or DeepSeek peak hours starting); work so far is in {repo}.)"
        claim_clear_pid()
    out = open(live_log).read()
    text = claude_result(out)
    if note_worker_limit('deepti', stream_error_text(out, err)):
        raise RuntimeError(f'Deepti hit an API limit/auth/credit error: {(text or err or out)[-500:]}')
    if proc.returncode != 0 and not text:
        raise RuntimeError(f'deepti rc={proc.returncode}: {(err or out)[-500:]}')
    return text or '(no output)'


def run_hermes(profile, project, prompt):
    """Oscar (default profile) or Vicky (paula profile) via the local Hermes api_server; one session per room."""
    envf = '/root/.hermes/.env' if not profile else f'/root/.hermes/profiles/{profile}/.env'
    env = {}
    for line in open(envf):
        if line.startswith('API_SERVER_') and '=' in line:
            k, v = line.strip().split('=', 1); env[k] = v.strip().strip('"').strip("'")
    # Port comes from the default profile's .env (profiles are routed as /p/<name> on the same server).
    # 2026-11-05: was hardcoded 8642 (VPS); Hermes on CT 132 listens on 8081.
    port = '8642'
    for line in open('/root/.hermes/.env'):
        if line.startswith('API_SERVER_PORT='):
            port = line.strip().split('=', 1)[1].strip().strip('"').strip("'")
    base = f'http://127.0.0.1:{port}' + (f'/p/{profile}' if profile else '')
    r = requests.post(f'{base}/v1/chat/completions',
                      headers={'Authorization': f"Bearer {env['API_SERVER_KEY']}",
                               'X-Hermes-Session-Id': f'talk-{project}'},
                      json={'model': env.get('API_SERVER_MODEL_NAME', 'hermes-agent'),
                            'messages': [{'role': 'user', 'content': prompt}]}, timeout=1800)
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content']


MUSE_TOOL = f'{HOME}/tools/muse'


def run_muse(project, task):
    """Meta Muse is external (Meta servers): it gets only the raw message text, no thread context,
    and only in rooms flagged "muse" (Manuel 2026-10-24: public material only). tools/muse holds a lock."""
    if not CFG['projects'].get(project, {}).get('muse') and not CFG.get('specialists'):
        return 'oscar', 'Muse only works in the Muse room (public material only); ask there.'
    text = MENTION_RE.sub('', re.sub(r'^.*? wrote: ', '', task, count=1, flags=re.S)).strip()
    try:
        r = subprocess.run([MUSE_TOOL, text], capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return 'oscar', 'Muse did not answer within 30 minutes.'
    out = r.stdout.strip()
    if r.returncode == 2:
        return 'oscar', ('Muse is offline: Brave with Muse is not running on the VM. '
                         '@mrar1995 please double-click "Meta Muse (Brave)" on the VM desktop, then ask again.')
    if r.returncode == 4:
        return 'muse', out + ('\n\n[Muse is waiting for an approval in its web app. muse_autoapprove allows review-test '
                              'uploads; anything else reaches Manuel on Telegram with Allow/Deny buttons.]')
    if r.returncode != 0:
        return 'muse' if out else 'oscar', (out + '\n\n' if out else '') + f'[muse tool: {r.stderr.strip()[-300:]}]'
    return 'muse', out or '(Muse sent an empty answer)'


def execute(agent, project, task, context, notify, thread_key=None):
    """Run a task with routing/fallback. Returns (agent_that_ran, text, ringo_session_record)."""
    if agent == 'muse':
        return run_muse(project, task) + (None,)
    if agent in HERMES_AGENTS:
        with HERMES_LOCKS[(agent, project)]:
            return agent, run_hermes(HERMES_AGENTS[agent], project, build_prompt(agent, project, task, context)), None
    if agent == 'freddy' and deepti_for(project):
        notify('Freddy is paused; Deepti is taking this one.')
        agent = 'deepti'
    if agent == 'freddy':
        deadline = time.time() + CFG['brain']['wait_minutes'] * 60
        reason = ''
        while time.time() < deadline:
            if os.path.exists(PAUSE_FLAG):
                reason = 'Freddy/Brain paused by Manuel'; break
            ok, reason = brain_available()
            if ok and FREDDY_LOCK.acquire(blocking=False):
                try:
                    return 'freddy', run_freddy_rounds(project, task, context, notify), None
                except FreddyYield as e:
                    prim, rnd, partial = e.args
                    repo = CFG['projects'][project]['repo']
                    return ('oscar', f'Freddy paused this card after round {rnd} because {prim} (his primary project) '
                            f'has a card ready. His edits stay uncommitted in {repo}: the next run continues from '
                            '`git status` / `git diff` there, not from scratch.'
                            + (f'\nHis last output was:\n{partial[-1200:]}' if partial.strip() else ''), 'REQUEUE')
                except FreddyStopped as e:
                    who, handoff, partial = e.args
                    if not handoff:
                        return 'oscar', f'Freddy\'s task was cancelled by {who}. Nothing was handed over.', None
                    notify(f'Freddy was stopped by {who}; Miku is taking over this task.')
                    repo = CFG['projects'][project]['repo']
                    task = (task + '\n\nNOTE: Freddy started this task and was stopped part-way. '
                            + (f'Run `git status` / `git diff` in {repo} first and continue from his edits '
                               'rather than starting over. ' if repo else '')
                            + (f'His last output was:\n{partial}' if partial.strip() else ''))
                    agent = 'miku'; break
                except Exception as e:
                    reason = f'Freddy failed: {e}'; log.error(reason); break
                finally:
                    FREDDY_LOCK.release()
            time.sleep(30)
        if agent == 'freddy':
            if CFG['projects'][project].get('miku_fallback', True):
                notify(f'{reason or "Freddy busy"}; Miku is taking this one.')
                agent = 'miku'
            else:
                return ('oscar', f"{reason or 'Freddy is busy'}. Miku is reserved for Oscar/Vicky dev work, "
                        "not LucidMail/DocReview, so this just waits for Freddy; ask again once he's free.",
                        'FAILED' if reason.startswith('Freddy failed') else 'REQUEUE')
    if (agent == 'miku' and CFG['agents']['miku'].get('reassign_to')
            and '[urgent]' in (task or '').lower() and FREDDY_LOCK.locked() and GEM_LOCK.locked()):
        log.info('miku urgent overflow: freddy and gem both busy, project=%s', project)   # Manuel 2026-11-08
    elif agent == 'miku' and CFG['agents']['miku'].get('reassign_to'):   # Manuel 2026-11-08: no Claude tokens for Miku
        tgt = CFG['agents']['miku']['reassign_to']
        if project in CFG['agents'][tgt].get('projects', []):
            log.info('miku disabled: %s takes project=%s', tgt, project)
            agent = tgt
        else:
            return ('oscar', f"Miku is switched off (Manuel 2026-11-08, saves Claude subscription tokens) and {display(tgt)} "
                    f"is not allowed on {project}. This waits for Freddy or Deepti.", 'REQUEUE')
    if agent == 'deepti':
        if project not in CFG['agents']['deepti'].get('projects', []):
            return ('oscar', f"Deepti is not allowed on {project} (DeepSeek: our own apps only, no work or family data). "
                    'Give it to @freddy instead.', None)
        if deepti_seconds_left() < 300:
            return ('oscar', 'Deepti only works in DeepSeek off-peak hours (peak = Mon-Fri 01-04 and 06-10 UTC, double price); '
                    'this waits for off-peak or for Freddy.', 'REQUEUE')
        with DEEPTI_LOCK:
            try:
                return 'deepti', run_deepti(project, build_prompt('deepti', project, task, context)), None
            except Exception as e:
                log.error('deepti run failed project=%s: %s', project, str(e)[:300])
                return 'deepti', f'(Deepti run failed: {e})', 'FAILED'
    if agent == 'gem':
        if project not in CFG['agents']['gem'].get('projects', []):
            return ('oscar', f"Gem is not allowed on {project}. Give it to @freddy instead.", None)
        with GEM_LOCK:
            try:
                return 'gem', run_gem(project, build_prompt('gem', project, task, context)), None
            except Exception as e:
                log.error('gem run failed project=%s: %s', project, str(e)[:300])
                return 'gem', f'(Gem run failed: {e})', 'FAILED'
    if agent == 'argon':
        if project not in CFG['agents'].get('argon', {}).get('projects', []):
            return ('oscar', f"Argon is not allowed on {project}. Give it to @ringo instead.", None)
        if agent_limited('argon'):   # 2026-11-09: quota gate closed: skip the instant-reject run, go straight to the next rung
            log.info('argon quota gate closed until %s, going straight to ringo for project=%s',
                     hhmm(STATE.get('agy_limit_until_argon') or 0), project)
            with RINGO_LOCKS[project]:
                return ('ringo',) + run_claude('ringo', project, build_prompt('ringo', project, task, context), thread_key)
        if not ARGON_LOCK.acquire(blocking=False):
            log.info('argon busy, falling back to ringo for project=%s', project)
            with RINGO_LOCKS[project]:
                return ('ringo',) + run_claude('ringo', project, build_prompt('ringo', project, task, context), thread_key)
        try:
            return 'argon', run_gem(project, build_prompt('argon', project, task, context), agent='argon'), None
        except Exception as e:
            log.error('argon run failed project=%s: %s', project, str(e)[:300])
            log.info('argon failed, falling back to ringo for project=%s', project)
            try:
                with RINGO_LOCKS[project]:
                    return ('ringo',) + run_claude('ringo', project, build_prompt('ringo', project, task, context), thread_key)
            except Exception as e2:
                log.error('fallback ringo also failed: %s', e2)
                return 'argon', f'(Argon run failed: {e})', 'FAILED'
        finally:
            ARGON_LOCK.release()
    if agent == 'caine':
        if project not in CFG['agents']['caine'].get('projects', []):
            return ('oscar', f"Caine is not allowed on {project}: his free Inkling tier trains on what it sees. "
                    'Give it to @freddy instead.', None)
        with CAINE_LOCK:
            try:
                return 'caine', run_caine(project, build_prompt('caine', project, task, context)), None
            except Exception as e:
                log.error('caine run failed project=%s: %s', project, str(e)[:300])
                return 'caine', f'(Caine run failed: {e})', 'FAILED'
    try:   # ClaudeLimit propagates: callers hold the work until the reset
        if agent == 'miku':
            with MIKU_SEM:
                return ('miku',) + run_claude('miku', project, build_prompt('miku', project, task, context))[:1] + (None,)
        with RINGO_LOCKS[project]:
            return ('ringo',) + run_claude('ringo', project, build_prompt('ringo', project, task, context), thread_key)
    except ClaudeFailed as e:
        log.error('%s run failed project=%s: %s', agent, project, str(e)[:300])
        return agent, f'({display(agent)} run failed: {e})', 'FAILED'

# ---------------------------------------------------------------- ringo review

REVIEW_MAX_FIX_ROUNDS = CFG.get('review_max_fix_rounds', 2)
VERDICTS = ('APPROVED', 'FIXES', 'TOOK_OVER', 'NEEDS_MANUEL')


def parse_verdict(text):
    lines = (text or '').strip().splitlines()
    head = lines[0].strip().strip('*#: ').upper() if lines else ''
    for v in VERDICTS:
        if head.startswith(v):
            rest = lines[0].strip().lstrip('*#').strip()[len(v):].strip(' :-—*')
            return v, '\n'.join(([rest] if rest else []) + lines[1:]).strip()
    return 'NEEDS_MANUEL', text or '(empty review)'


def display(name):
    """Display name for any poster/agent, including non-roster ones like 'oscar'."""
    return CFG['agents'].get(name, {}).get('display') or name.title()


# ---------------------------------------------------------------- Definition of Ready (TASK4 part C)
# A card may only enter To do when it has numbered acceptance steps, a priority line, a project and a
# non-conflicting label set. Existing To do cards are grandfathered once, when the flag is first enabled.
ACCEPTANCE_STEP_RE = re.compile(r'^\s*(?:\d+[.)]|[-*]\s*\d+[.)])\s+\S', re.M)
PRIORITY_RE = re.compile(r'^\s*(?:[-*]\s*)?(?:priority|prio)\b|^\s*(?:[-*]\s*)?P[0-3]\b', re.I | re.M)
PROJECT_RE = re.compile(r'^\s*(?:[-*]\s*)?project\s*[:=-]\s*(\S.*)$', re.I | re.M)
GRANDFATHER_RE = re.compile(r'^\s*scrum_grandfather\s*:\s*true\s*$', re.I | re.M)


def _worker_label_titles():
    titles = {display(n) for n in CFG.get('agents', {}) if n not in HERMES_AGENTS}
    titles.add('Argon')
    return titles


def _ready_missing(card):
    missing = []
    desc = card.get('description') or ''
    if len(ACCEPTANCE_STEP_RE.findall(desc)) < 2:
        missing.append('acceptance steps')
    if not PRIORITY_RE.search(desc):
        missing.append('priority')
    project = card.get('project') or ''
    if not project and not PROJECT_RE.search(desc):
        missing.append('project')
    worker_labels = [l.get('title') for l in card.get('labels') or []
                     if isinstance(l, dict) and l.get('title') in _worker_label_titles()]
    if len(worker_labels) > 1:
        missing.append('labels')
    return missing


def is_ready(card):
    """(True, []) when a To spec card may move to To do, else (False, [missing items])."""
    missing = _ready_missing(card or {})
    return (not missing, missing)


def is_grandfathered(card):
    """True for cards that were already in To do when the DoR gate was enabled."""
    card = card or {}
    if card.get('scrum_grandfather') is True:
        return True
    if str(card.get('scrum_grandfather')).lower() == 'true':
        return True
    labels = {str((l or {}).get('title') or '').lower() for l in card.get('labels') or []}
    if labels & {'scrum_grandfather', 'grandfathered'}:
        return True
    if GRANDFATHER_RE.search(card.get('description') or ''):
        return True
    return bool(STATE.get('scrum_grandfather', {}).get(str(card.get('id'))))


def grandfather_current_todo(board_stacks):
    """One-shot: mark every card already in To do as ready-grandfathered (never re-marks new cards)."""
    if not scrum_enabled() or STATE.get('scrum_grandfather_done'):
        return
    marked = STATE.setdefault('scrum_grandfather', {})
    for all_stacks in (board_stacks or {}).values():
        for stack in all_stacks or []:
            if stack.get('title') == 'To do':
                for card in stack.get('cards') or []:
                    marked[str(card.get('id'))] = True
    configured = sum(len(p.get('boards', [])) for p in CFG.get('projects', {}).values())
    if len(board_stacks or {}) >= configured:
        STATE['scrum_grandfather_done'] = True   # partial board failures re-check next poll
    save_state()


def return_card_to_to_spec(project, board, all_stacks, card, missing, agent=None):
    """A pull worker refuses an unready card: comment what is missing and put it back in To spec."""
    cid = str(card['id'])
    stacks_map = {s['title']: s['id'] for s in all_stacks or []}
    to_spec = stacks_map.get('To spec')
    if not to_spec:
        try:
            to_spec = ensure_stack(board, 'To spec')
        except Exception:
            log.error('could not create To spec on board %s for card %s', board, cid)
            return False
    if not to_spec:
        log.error('no To spec stack on board %s for card %s', board, cid)
        return False
    text = ('[Definition of Ready] Not pulled: missing ' + ', '.join(missing)
            + '. The card stays in To spec until the spec lane adds those items.')
    try:
        deck_comment('oscar', card['id'], text)
        current = card.get('stackId') or stacks_map.get('To do')
        deck('oscar', 'PUT', f"/boards/{board}/stacks/{current}/cards/{cid}/reorder",
             json={'order': 0, 'stackId': to_spec})
        card['stackId'] = to_spec
        # Do not bounce straight back into the spec lane: give the spec a cool-down to be fixed.
        STATE.setdefault('spec_failed', {})[cid] = time.time()
        save_state()
        log.info('DoR: card %s refused by %s on %s: %s', cid, agent or '?', project, ', '.join(missing))
        if scrum_enabled():
            note_card_return(project, board, all_stacks, card,
                             'Definition of Ready: missing ' + ', '.join(missing), agent, already_to_spec=True)
        return True
    except Exception:
        log.error('returning unready card %s to To spec failed\n%s', cid, traceback.format_exc())
        return False


# ---------------------------------------------------------------- Definition of Done (TASK4 part D)
# The Done move is refused while any required evidence is missing. Evidence is plain data recorded on the
# card by the worker (description fields/markers) or passed explicitly by the caller. Each gate result is
# commented on the card so the evidence is auditable afterwards.
DEFAULT_WIP = {'per_worker_doing': 1, 'per_project_to_review': 3, 'per_project_to_test': 3}
RETURN_ESCALATE_AT = 3


def wip_limits(cfg=None):
    """company.json `wip` limits, with the TASK4 defaults when the key is absent."""
    cfg = CFG if cfg is None else cfg
    raw = cfg.get('wip') or {}

    def _int(default, *keys):
        for key in keys:
            if key not in raw:
                continue
            try:
                return max(0, int(raw[key]))
            except (TypeError, ValueError):
                continue
        return default

    return {'per_worker_doing': _int(1, 'per_worker_doing', 'worker_doing', 'per_worker'),
            'per_project_to_review': _int(3, 'per_project_to_review', 'to_review', 'per_project_review'),
            'per_project_to_test': _int(3, 'per_project_to_test', 'to_test', 'per_project_test')}


def _project_cfg(project, cfg=None):
    return ((CFG if cfg is None else cfg).get('projects') or {}).get(project) or {}


def project_has_ci(project, cfg=None):
    """A project has CI when company.json gives it a ci_cmd; missing = the gate is skipped."""
    return bool(_project_cfg(project, cfg).get('ci_cmd'))


def project_has_deploy(project, cfg=None):
    p = _project_cfg(project, cfg)
    return bool(p.get('deploy_cmd') or p.get('deploy'))


def project_requires_muse(project, cfg=None):
    """True when the project's cards must pass the To test / Muse lane before Done."""
    cfg = CFG if cfg is None else cfg
    p = _project_cfg(project, cfg)
    if 'muse' in p:
        return bool(p.get('muse'))
    if 'requires_muse' in p:
        return bool(p.get('requires_muse'))
    return bool((cfg.get('muse_targets') or {}).get(project))


def _truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'y', 'ok', 'done', 'pass', 'passed',
                                                'green', 'approved', 'merge', 'merged', 'deployed')


def _exit_ok(value):
    if isinstance(value, bool):
        return value
    try:
        return int(value) == 0
    except (TypeError, ValueError):
        return False


_EXIT_RE = {
    'test_exit': re.compile(r'\btests?\b[^\n]{0,40}?\bexit(?:\s*code)?\s*[:=]\s*(-?\d+)', re.I),
    'ci_exit': re.compile(r'\bci\b[^\n]{0,40}?\bexit(?:\s*code)?\s*[:=]\s*(-?\d+)', re.I),
    'deploy_exit': re.compile(r'\bdeploy(?:ed|ment)?\b[^\n]{0,40}?\bexit(?:\s*code)?\s*[:=]\s*(-?\d+)', re.I),
}


def card_done_evidence(card):
    """Evidence a worker recorded on the card: explicit fields, card['evidence'], or description markers."""
    card = card or {}
    ev = {}
    if isinstance(card.get('evidence'), dict):
        ev.update(card['evidence'])
    for key in ('test_exit', 'tests_exit', 'ci_exit', 'deploy_exit'):
        if key in card:
            ev['test_exit' if key == 'tests_exit' else key] = card[key]
    for key in ('tests_green', 'tests_passed', 'ci_green', 'review_approved', 'merged', 'deployed',
                'muse_passed', 'review'):
        if key in card:
            ev[key] = card[key]
    desc = str(card.get('description') or '')
    for key, rx in _EXIT_RE.items():
        m = rx.search(desc)
        if m and key not in ev:
            try:
                ev[key] = int(m.group(1))
            except ValueError:
                pass
    if 'ci_green' not in ev:
        m = re.search(r'\bci\b[^\n]{0,40}\b(?:green|pass|passed|ok)\b', desc, re.I)
        if m:
            ev['ci_green'] = True
    if 'review_approved' not in ev and re.search(
            r'\breview\b[^\n]{0,40}\bapproved\b|\bapproved\b[^\n]{0,30}\breview\b', desc, re.I):
        ev['review_approved'] = True
    if 'merged' not in ev and re.search(r'\bmerged\b[^\n]{0,20}\b(?:yes|true|ok|done)\b', desc, re.I):
        ev['merged'] = True
    if 'deployed' not in ev and re.search(
            r'\bdeploy(?:ed|ment)?\b[^\n]{0,30}\b(?:ok|yes|true|done|green|exit\s*0)\b', desc, re.I):
        ev['deployed'] = True
    if 'muse_passed' not in ev and re.search(
            r'\bmuse(?: test)?\b[^\n]{0,30}\b(?:pass|passed|green|ok)\b', desc, re.I):
        ev['muse_passed'] = True
    return ev


def done_gate_results(card, evidence=None, project=None, cfg=None):
    """One result per gate: {'gate', 'ok', 'detail', 'skipped'}. Pure; no Deck calls."""
    cfg = CFG if cfg is None else cfg
    ev = card_done_evidence(card)
    ev.update(evidence or {})
    project = project or (card or {}).get('project') or ''
    results = []

    tests_ok = _exit_ok(ev.get('test_exit')) or _truthy(ev.get('tests_green')) \
        or _truthy(ev.get('tests_passed'))
    results.append({'gate': 'tests', 'ok': tests_ok,
                    'detail': (f"exit {ev['test_exit']}" if 'test_exit' in ev
                               else 'no test exit code recorded')})

    if project_has_ci(project, cfg):
        ci_ok = _exit_ok(ev.get('ci_exit')) or _truthy(ev.get('ci_green'))
        results.append({'gate': 'CI', 'ok': ci_ok,
                        'detail': (f"exit {ev['ci_exit']}" if 'ci_exit' in ev else 'CI not green')})
    else:
        results.append({'gate': 'CI', 'ok': True, 'detail': 'skipped: project has no ci_cmd',
                        'skipped': True})

    review_ok = _truthy(ev.get('review_approved')) or \
        str(ev.get('review') or '').strip().lower() in ('approved', 'approve', 'pass', 'passed')
    results.append({'gate': 'review', 'ok': review_ok,
                    'detail': 'approved' if review_ok else 'review not approved'})

    merged_ok = _truthy(ev.get('merged'))
    results.append({'gate': 'merged', 'ok': merged_ok,
                    'detail': 'merged' if merged_ok else 'not merged'})

    if project_has_deploy(project, cfg):
        deployed_ok = _truthy(ev.get('deployed')) or _exit_ok(ev.get('deploy_exit'))
        results.append({'gate': 'deploy', 'ok': deployed_ok,
                        'detail': 'deployed' if deployed_ok else 'not deployed'})
    else:
        results.append({'gate': 'deploy', 'ok': True, 'detail': 'skipped: project has no deploy step',
                        'skipped': True})

    if project_requires_muse(project, cfg):
        muse_ok = _truthy(ev.get('muse_passed'))
        results.append({'gate': 'Muse', 'ok': muse_ok,
                        'detail': 'passed' if muse_ok else 'Muse test not passed'})
    else:
        results.append({'gate': 'Muse', 'ok': True, 'detail': 'skipped: project does not require Muse',
                        'skipped': True})
    return results


def is_done(card, evidence, project=None, cfg=None):
    """(True, []) when every required Definition of Done gate has evidence; else (False, [missing])."""
    results = done_gate_results(card, evidence, project, cfg)
    missing = [r['gate'] for r in results if not r['ok']]
    return (not missing, missing)


def record_done_gates(card_id, results):
    """Record every gate result as its own Deck comment (auditable evidence trail)."""
    for r in results:
        state = 'PASS' if r['ok'] else 'MISSING'
        suffix = ' (skipped)' if r.get('skipped') else ''
        try:
            deck_comment('oscar', card_id,
                         f"[Definition of Done] {r['gate']}: {state}{suffix} - {r['detail']}")
        except Exception:
            log.error('recording Done gate %s on card %s failed\n%s',
                      r.get('gate'), card_id, traceback.format_exc())


def _stack_title_for_id(all_stacks, stack_id, default='its stack'):
    for stack in all_stacks or []:
        if str(stack.get('id')) == str(stack_id):
            return stack.get('title') or default
    return default


def move_card_to_done(project, board, stacks, card, evidence=None, session_agent=None, all_stacks=None):
    """Scrum Done gate. Flag off: move exactly as before. Flag on: refuse while evidence is missing."""
    cid = str(card['id'])
    done_id = stacks.get('Done') or stacks.get('Finished')
    source = card.get('stackId') or stacks.get('To test') or stacks.get('Doing')
    session_agent = session_agent or ('oscar' if int(board) in (27, 30) else 'ringo')
    if not scrum_enabled():
        if not done_id:
            return False
        deck(session_agent, 'PUT', f"/boards/{board}/stacks/{source}/cards/{cid}/reorder",
             json={'order': 0, 'stackId': done_id})
        return True

    ev = card_done_evidence(card)
    ev.update(evidence or {})
    results = done_gate_results(card, ev, project)
    record_done_gates(cid, results)
    missing = [r['gate'] for r in results if not r['ok']]
    if missing:
        here = _stack_title_for_id(all_stacks, card.get('stackId'))
        deck_comment('oscar', cid,
                     '[Definition of Done] Move to Done blocked; missing: ' + ', '.join(missing)
                     + f'. The card stays in {here}.')
        log.info('DoD: card %s stays in %s (missing %s)', cid, here, ', '.join(missing))
        return False
    if not done_id:
        return False
    deck(session_agent, 'PUT', f"/boards/{board}/stacks/{source}/cards/{cid}/reorder",
         json={'order': 0, 'stackId': done_id})
    log.info('DoD: card %s passed every gate -> Done', cid)
    return True


# ---------------------------------------------------------------- WIP limits (TASK4 part E)
def _iter_project_stacks(board_stacks, cfg=None):
    """Yield (project, board, all_stacks) in company.json project order."""
    order = {name: i for i, name in enumerate((CFG if cfg is None else cfg).get('projects', {}))}
    for (project, board), all_stacks in sorted(
            (board_stacks or {}).items(), key=lambda kv: (order.get(kv[0][0], 999), str(kv[0][1]))):
        yield project, board, all_stacks or []


def _stack_title(stack):
    return str((stack or {}).get('title') or '').strip().lower()


def project_wip(board_stacks, project, cfg=None):
    """Current cards per state for a project: To review, To test and Doing."""
    counts = {'to_review': 0, 'to_test': 0, 'doing': 0}
    for proj, _board, all_stacks in _iter_project_stacks(board_stacks, cfg):
        if proj != project:
            continue
        for stack in all_stacks:
            title = _stack_title(stack)
            n = len([c for c in stack.get('cards') or [] if isinstance(c, dict)])
            if title == 'to review':
                counts['to_review'] += n
            elif title == 'to test':
                counts['to_test'] += n
            elif title == 'doing':
                counts['doing'] += n
    return counts


def wip_hit(board_stacks, project, cfg=None):
    """(True, reasons) when the project is at a To review / To test WIP limit."""
    limits = wip_limits(cfg)
    counts = project_wip(board_stacks, project, cfg)
    reasons = []
    if limits['per_project_to_review'] and counts['to_review'] >= limits['per_project_to_review']:
        reasons.append(f"To review {counts['to_review']}/{limits['per_project_to_review']}")
    if limits['per_project_to_test'] and counts['to_test'] >= limits['per_project_to_test']:
        reasons.append(f"To test {counts['to_test']}/{limits['per_project_to_test']}")
    return (bool(reasons), reasons)


def _agent_from_labels(card):
    titles = {str((l or {}).get('title') or '') for l in (card or {}).get('labels') or []}
    for name, agent in CFG.get('agents', {}).items():
        if agent.get('display') in titles:
            return name
    return ''


def _doing_worker(card, state):
    worker = _agent_from_labels(card)
    if worker:
        return worker
    cid = str(card.get('id'))
    claim = (state.get('claims') or {}).get(cid) or {}
    if claim.get('worker'):
        return str(claim['worker'])
    active = str((state.get('deck_active') or {}).get(cid) or '')
    return active.split('@', 1)[0] if active else ''


def worker_doing_counts(board_stacks, state=None, cfg=None):
    state = STATE if state is None else state
    counts = {}
    for _project, _board, all_stacks in _iter_project_stacks(board_stacks, cfg):
        for stack in all_stacks:
            if _stack_title(stack) != 'doing':
                continue
            for card in stack.get('cards') or []:
                labels = {str((l or {}).get('title') or '') for l in card.get('labels') or []}
                if labels & EXCLUDED_PULL_LABELS:
                    continue   # pending/hold labels (Decide, Manuel, To review...) are
                               # a decision or a hold, not live work: they must not fill a lane
                worker = _doing_worker(card, state)
                if worker:
                    counts[worker] = counts.get(worker, 0) + 1
    return counts


def worker_wip_hit(agent, board_stacks, state=None, cfg=None):
    limit = wip_limits(cfg)['per_worker_doing']
    if not limit:
        return False
    return worker_doing_counts(board_stacks, state, cfg).get(agent, 0) >= limit


# ---------------------------------------------------------------- self-organizing pull hooks (TASK4 part G)
SELF_ORGANIZING_ORDER = ('review', 'test', 'unblock', 'improvement')
_UNBLOCK_MARKER = 'Returned to To Do, due to worker unavailability'


def _pullable_card(card, state, frozen):
    cid = str(card.get('id'))
    if cid in (state.get('deck_active') or {}):
        return False
    if frozen is not None and cid not in frozen:
        return False
    # A card the DoR gate just sent back to To spec gets the same 6 h cool-down as the spec lane.
    failed_at = (state.get('spec_failed') or {}).get(cid)
    if isinstance(failed_at, (int, float)) and time.time() - failed_at < 6 * 3600:
        return False
    labels = {str((l or {}).get('title') or '') for l in card.get('labels') or []}
    return not (labels & EXCLUDED_PULL_LABELS)


def _agent_may_take(agent, project, cfg=None):
    p = _project_cfg(project, cfg)
    if not p.get('repo'):
        return False
    if p.get('outside_work_hours') and in_work_hours():
        return False
    if agent in ('caine', 'gem', 'deepti'):
        allowed = ((CFG if cfg is None else cfg).get('agents', {}).get(agent) or {}).get('projects') or []
        if project not in allowed:
            return False
    return True


def _project_in_flight(project, state):
    active = [str(v) for v in (state.get('deck_active') or {}).values()]
    recover = [str((r or {}).get('who') or '') for r in (state.get('recover') or {}).values()]
    return any(v.endswith('@' + project) for v in active + recover)


def _stack_cards(all_stacks, title):
    for stack in all_stacks:
        if _stack_title(stack) == title.lower():
            return [c for c in stack.get('cards') or [] if isinstance(c, dict)]
    return []


def _return_marked(card, state):
    cid = str(card.get('id'))
    if int((state.get('card_returns') or {}).get(cid, 0) or 0) >= 1:
        return True
    return _UNBLOCK_MARKER in str(card.get('description') or '')


def pick_self_organizing_task(agent, board_stacks, state=None, frozen=None, cfg=None,
                              include_improvement=True, kinds=None):
    """Free worker with an empty queue, in order: review > test > unblock > improvement cards.

    Returns {'kind', 'project', 'board', 'stacks', 'card'} or None. Pure selection: no Deck calls.
    """
    state = STATE if state is None else state
    cfg = CFG if cfg is None else cfg
    if kinds is None:
        kinds = SELF_ORGANIZING_ORDER if include_improvement else SELF_ORGANIZING_ORDER[:3]
    for kind in kinds:
        for project, board, all_stacks in _iter_project_stacks(board_stacks, cfg):
            if not _agent_may_take(agent, project, cfg):
                continue
            if _project_in_flight(project, state):
                continue
            if kind == 'review':
                cards = _stack_cards(all_stacks, 'To review')
            elif kind == 'test':
                cards = _stack_cards(all_stacks, 'To test')
            elif kind == 'unblock':
                cards = _stack_cards(all_stacks, 'To do') + _stack_cards(all_stacks, 'To spec')
            else:
                cards = _stack_cards(all_stacks, 'To spec') + _stack_cards(all_stacks, 'To do')
            for card in cards:
                if not _pullable_card(card, state, frozen):
                    continue
                if kind == 'unblock' and not _return_marked(card, state):
                    continue
                if kind == 'improvement':
                    labels = {str((l or {}).get('title') or '') for l in card.get('labels') or []}
                    if 'Improvement' not in labels:
                        continue
                return {'kind': kind, 'project': project, 'board': board,
                        'stacks': all_stacks, 'card': card}
    return None


def pick_finishing_task(agent, board_stacks, state=None, frozen=None, cfg=None):
    """Review/test/unblock only (TASK4 part E's stop-starting-start-finishing redirect)."""
    return pick_self_organizing_task(agent, board_stacks, state, frozen, cfg,
                                     include_improvement=False)


def _stacks_map(stacks):
    if isinstance(stacks, dict):
        return dict(stacks)
    return {s['title']: s['id'] for s in stacks or [] if isinstance(s, dict) and s.get('title')}


def note_card_return(project, board, stacks, card, reason, agent=None, already_to_spec=False):
    """Count each return per card. At 3 the card is relabelled Argon and sent to To spec for a better spec.

    This is never a decision card for Manuel. Flag off: no state change, exactly the old behaviour.
    """
    if not scrum_enabled():
        return 0
    cid = str(card['id'])
    with _lock:
        counts = STATE.setdefault('card_returns', {})
        counts[cid] = int(counts.get(cid, 0) or 0) + 1
        count = counts[cid]
    save_state()
    if count != RETURN_ESCALATE_AT:
        return count
    try:
        smap = _stacks_map(stacks)
        to_spec = smap.get('To spec')
        if not to_spec:
            to_spec = ensure_stack(board, 'To spec')
        ensure_label(board, 'Argon')
        b = deck('oscar', 'GET', f'/boards/{board}')
        blabels = {l['title']: l['id'] for l in (b.get('labels', []) if isinstance(b, dict) else [])
                   if isinstance(l, dict) and 'title' in l and 'id' in l}
        current = to_spec if already_to_spec else (card.get('stackId') or to_spec)
        if already_to_spec and to_spec:
            card['stackId'] = to_spec
        if 'Argon' in blabels:
            deck('oscar', 'PUT', f"/boards/{board}/stacks/{current}/cards/{cid}/assignLabel",
                 json={'labelId': blabels['Argon']})
        if to_spec and not already_to_spec:
            deck('oscar', 'PUT', f"/boards/{board}/stacks/{current}/cards/{cid}/reorder",
                 json={'order': 0, 'stackId': to_spec})
            card['stackId'] = to_spec
        deck_comment('oscar', cid,
                     f'[Return limit] This card needs a better spec: it was returned {count} times. '
                     'It was relabelled Argon and sent back to To spec. This is not a decision for Manuel.')
        with _lock:
            STATE.setdefault('escalation_log', []).append(
                {'cid': cid, 'title': card.get('title') or '', 'project': project, 'board': board,
                 'agent': agent or '', 'reason': f'returned {count} times; needs a better spec',
                 'at': time.time()})
            _prune_logs(STATE)
        save_state()
        log.warning('card %s returned %s times: relabelled Argon and sent to To spec', cid, count)
    except Exception:
        log.error('return-limit escalation for card %s failed\n%s', cid, traceback.format_exc())
    return count


def _append_scrum_event(key, entry):
    """Append a timeline entry (failure_log / quota_log) only while the Scrum flag is on."""
    if not scrum_enabled():
        return
    with _lock:
        STATE.setdefault(key, []).append(entry)
        _prune_logs(STATE)
    save_state()


TOOK_OVER_MAX_LINES = 20


def _tree_id(repo):
    """Tree id of the working tree, untracked (non-ignored) files included, built in a throwaway index so the
    repo is untouched. 2026-10-26: `git stash create` left out untracked files, so a coder's new files that the
    reviewer committed counted as the reviewer's lines (card 361: 2106 "changed" lines, OVER LIMIT)."""
    if not repo:
        return None
    fd, idx = tempfile.mkstemp(prefix='snap-index-')
    os.close(fd)
    os.unlink(idx)
    env = {**os.environ, 'GIT_INDEX_FILE': idx}
    g = lambda *a: subprocess.run(['git', '-C', repo, *a], capture_output=True, text=True, timeout=60, env=env)
    try:
        if g('add', '-A').returncode:
            return None
        return g('write-tree').stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if os.path.exists(idx):
            os.unlink(idx)


def tree_snapshot(repo):
    return _tree_id(repo)


def changed_lines(repo, base):
    """Lines added+deleted between the base snapshot and the working tree now (commits, edits, new files)."""
    now = _tree_id(repo) if base else None
    if not now:
        return None
    try:
        out = subprocess.run(['git', '-C', repo, 'diff', '--numstat', base, now], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return sum(int(a) + int(b) for a, b, *_ in (l.split('\t') for l in out.splitlines()) if a.isdigit() and b.isdigit())


RINGO_EFFORT = threading.local()   # per-thread --effort override for Ringo (review_loop sets it)
LOW_EFFORT_MAX_LINES = 150          # Manuel 2026-10-30: reviews of smaller diffs run Ringo at low effort


def work_size(repo):
    """Changed lines of the coder's work: merge-base with main/master vs the working tree. None when unknown."""
    for br in ('main', 'master'):
        try:
            mb = subprocess.run(['git', '-C', repo, 'merge-base', 'HEAD', br], capture_output=True, text=True, timeout=30)
            if mb.returncode:
                continue
            out = subprocess.run(['git', '-C', repo, 'diff', '--numstat', mb.stdout.strip()],
                                 capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        return sum(int(a) + int(b) for a, b, *_ in (l.split('\t') for l in out.splitlines()) if a.isdigit() and b.isdigit())
    return None


def review_loop(project, task, coder, result, thread_key, notify):
    """Wraps _review_loop: Ringo reviews at low effort when the work is small and known (0 or unknown -> medium)."""
    repo = CFG['projects'][project]['repo']
    n = work_size(repo) if repo else None
    RINGO_EFFORT.value = 'low' if n and n <= LOW_EFFORT_MAX_LINES else 'medium'
    log.info('review effort project=%s key=%s lines=%s effort=%s', project, thread_key, n, RINGO_EFFORT.value)
    try:
        return _review_loop(project, task, coder, result, thread_key, notify)
    finally:
        RINGO_EFFORT.value = None


def _review_loop(project, task, coder, result, thread_key, notify):
    """Ringo reviews a coder's result; up to REVIEW_MAX_FIX_ROUNDS fix rounds.
    Returns (verdict, body, coder_that_ran_last, last_result).
    2026-10-25 (6 TOOK_OVERs, $12.52 of review in a day): TOOK_OVER only for trivial fixes or after Freddy failed
    twice on the same card (fix rounds counted across requeues in STATE['fix_rounds']); its diff size is logged."""
    repo = CFG['projects'][project]['repo']
    if claude_down():
        return ('NO_CLAUDE',
                'No-Claude mode: Ringo is offline, no review performed. The work is unreviewed in the repo '
                '(uncommitted or on its card branch). Integration to main and any Muse handoff wait for Ringo. '
                f'Interim check anyone can run: the project tests (e.g. scripts/test.sh in {repo}) and a look at the diff.',
                coder, result)
    fixes = STATE.setdefault('fix_rounds', {})
    for rnd in range(1, REVIEW_MAX_FIX_ROUNDS + 2):
        last = rnd > REVIEW_MAX_FIX_ROUNDS
        failed_twice = last or fixes.get(thread_key, 0) >= 2
        prompt = (f"REVIEW (round {rnd}). {display(coder)} just finished a task you own.\n\n"
                  f"Task:\n{task}\n\nTheir report:\n{result[:6000]}\n\n"
                  f"Check the real work, not just the report: in {repo} look at git status, git diff "
                  "(or git log -p for new commits) and run the relevant tests.\n"
                  "Start your reply with exactly one verdict word on its own first line:\n"
                  "APPROVED - correct and complete (add at most a line of notes).\n"
                  + ("" if last else "FIXES - precise, numbered fixes for the coder (file, what is wrong, a failing test or repro, how to verify). "
                     "This is the DEFAULT for anything wrong: Freddy's reruns are free, your time is expensive.\n")
                  + (f"TOOK_OVER - you fixed/finished it yourself in this turn. {display(coder)} has already failed twice on this task, so "
                     "you may take it over now; say what you changed and how you verified it.\n" if failed_twice else
                     f"TOOK_OVER - ONLY for a trivial fix you made yourself in this turn: about {TOOK_OVER_MAX_LINES} changed lines or fewer, "
                     "no new logic and no tests to design (typo, missing import, one-line condition). Anything bigger is FIXES. "
                     f"({display(coder)} has had {fixes.get(thread_key, 0)} fix round(s) on this task.) Say what you changed and how you verified it.\n")
                  + "NEEDS_MANUEL - a decision only Manuel can make; state it in one or two lines, then one line per choice as "
                  "'OPTION: <short choice>' when there are clear options.\n"
                  + ("This was the last fix round: FIXES is not allowed now.\n" if last else "")
                  + "Do not @mention anyone; the dispatcher routes your verdict."
                  + (f"\n\nAFTER APPROVED or TOOK_OVER, integrate in the same turn: {CFG['projects'][project]['integrate']}"
                     if CFG['projects'][project].get('integrate') else ""))
        base = tree_snapshot(repo)
        _, text, rflag = execute('ringo', project, prompt, '(review)', notify, thread_key=thread_key)  # ClaudeLimit propagates
        if rflag == 'FAILED':   # crashed review run: not a verdict
            log.info('review project=%s round=%s FAILED', project, rnd)
            return 'FAILED', text, coder, result
        verdict, body = parse_verdict(text)
        log.info('review project=%s round=%s verdict=%s', project, rnd, verdict)
        if verdict == 'FIXES' and last:
            verdict = 'NEEDS_MANUEL'
        if verdict == 'TOOK_OVER':
            n = changed_lines(repo, base)
            log.info('took_over project=%s key=%s changed_lines=%s failed_twice=%s%s', project, thread_key, n,
                     failed_twice, ' OVER LIMIT' if n and n > TOOK_OVER_MAX_LINES and not failed_twice else '')
            if n is not None:
                body += f'\n(Ringo changed {n} lines himself.)'
        if thread_key:
            with _lock:
                if verdict == 'FIXES':
                    fixes[thread_key] = fixes.get(thread_key, 0) + 1
                elif verdict in ('APPROVED', 'TOOK_OVER'):
                    fixes.pop(thread_key, None)
            save_state()
        if verdict != 'FIXES':
            return verdict, body, coder, result
        notify(f"Ringo asked {display(coder)} for fixes (round {rnd} of {REVIEW_MAX_FIX_ROUNDS}).")
        fix_task = f"{task}\n\nRingo reviewed your work and asks for these fixes (round {rnd}):\n{body}"
        ran, result, flag = execute(coder, project, fix_task, '(fix round)', notify)
        if flag in ('REQUEUE', 'FAILED') or ran not in CFG['agents']:
            # coder busy/paused/crashed: park the card in To do with Ringo's fixes as a comment;
            # the coder picks it up again later with the comments as context
            return 'REQUEUE', body, coder, result
        coder = ran
    return 'NEEDS_MANUEL', body, coder, result


# ---------------------------------------------------------------- task completion reports
# Manuel 2026-10-16: one Talk message per finished task he (or Oscar for him) asked for.
# A task = a root Talk message from a requester plus every job its @mention chain spawns. It is
# finished once no job of it has run for TASK_QUIET seconds: by then the poll that would spawn a
# follow-up job from the last reply has passed. A one-job task needs no extra message (the reply
# is the report and ends with "Pending:"); longer chains get a summary from Haiku, posted as Oscar.
# Deck cards report in deck_job. In memory only: jobs do not survive a restart either.
REQUESTERS = set(CFG.get('done_report_requesters', ['mrar1995', 'oscar']))
TASK_QUIET = 2 * CFG['poll_seconds'] + 5
TASKS = {}     # root mid -> {room, project, by, task, active, jobs, idle_since, log}
TASK_OF = {}   # posted mid -> root mid
TASK_LOCK = threading.Lock()


def task_open(root, project, room, m):
    with TASK_LOCK:
        TASK_OF[root] = root
        TASKS.setdefault(root, {'room': room, 'project': project, 'by': m['actorDisplayName'],
                                'task': render(m), 'active': 0, 'jobs': 0, 'idle_since': None, 'log': []})


def task_job_start(root):
    with TASK_LOCK:
        t = TASKS.get(root)
        if t:
            t['active'] += 1; t['jobs'] += 1; t['idle_since'] = None


def task_note(root, who, text, posted=None, extra_job=False):
    with TASK_LOCK:
        t = TASKS.get(root)
        if not t:
            return
        t['log'].append((who, text or ''))
        t['jobs'] += extra_job
        if posted:
            TASK_OF[posted] = root


def task_job_end(root):
    with TASK_LOCK:
        t = TASKS.get(root)
        if t:
            t['active'] -= 1
            if t['active'] <= 0:
                t['active'] = 0; t['idle_since'] = time.time()


def summarize(request, entries):
    """2-5 bullets of what was done + a final 'Pending:' line, by Haiku; plain fallback if it fails."""
    thread = '\n\n'.join(f'{display(w)}: {x[:4000]}' for w, x in entries)[-24000:]
    prompt = ("Summarise this finished task for Manuel as the body of ONE short chat message. "
              "Write 2-5 bullets on what was done (results, files changed, decisions, where things were written), "
              "then a last line 'Pending: <what is still open and who owns it, or a decision Manuel must make>' "
              "or 'Pending: nothing'. Use only facts from the thread. No heading, no @mentions, no tools. "
              "A coder's spec saying 'never push/merge/restart' binds the coder only: Ringo merging, deploying or restarting "
              "after his review is the normal integration step, not a violation and not a decision for Manuel.\n\n"
              f"Request:\n{request[:3000]}\n\nThread:\n{thread}")
    try:
        os.makedirs(f'{HOME}/work/_scratch', exist_ok=True)
        if claude_limited() or claude_down():
            raise ClaudeLimit('summary', claude_limited() or weekly_reset_est(), 'gate closed')
        rc, out, _ = run_cmd(['claude', '-p', '--model', 'haiku', '--output-format', 'json'],
                             f'{HOME}/work/_scratch', 300, stdin=prompt)
        ev = json.loads(out)
        reset = claude_limit_from(ev)
        if reset:
            note_limit('summary', reset, ev.get('result'))
        text = (ev.get('result') or '').strip() if rc == 0 and not ev.get('is_error') else ''
    except Exception as e:
        log.warning('summarize failed: %s', e); text = ''
    if not text:
        who, last = entries[-1] if entries else ('oscar', '')
        text = f'Last update from {display(who)}:\n{last[:1500]}'
        if 'pending:' not in last.lower():
            text += '\nPending: see the thread above.'
    return MENTION_RE.sub(lambda mm: mm.group(1).title(), text)


def finish_task(root, t):
    log.info('task done root=%s jobs=%s', root, t['jobs'])
    if t['jobs'] <= 1:
        return   # the single reply already is the report
    head = t['task'].strip().splitlines()[0] if t['task'].strip() else 'task'
    head = MENTION_RE.sub('', head).strip(' ,:')
    head = head[:120] + ('...' if len(head) > 120 else '')
    talk_post('oscar', t['room'], f'Task finished: "{head}" (asked by {t["by"]})\n'
              + summarize(t['task'], t['log']), reply_to=root, depth=99)


def check_tasks():
    now = time.time()
    with TASK_LOCK:
        done = [(r, t) for r, t in TASKS.items()
                if t['active'] == 0 and t['idle_since'] and now - t['idle_since'] >= TASK_QUIET]
        for r, _ in done:
            TASKS.pop(r)
        for k in [k for k, r in TASK_OF.items() if r in dict(done)]:
            TASK_OF.pop(k)
    for r, t in done:
        POOL.submit(_finish_safe, r, t)   # Haiku summary may take a while; keep polling


def _finish_safe(root, t):
    try:
        finish_task(root, t)
    except Exception:
        log.error('finish_task failed\n%s', traceback.format_exc())


def needs_review(project, ran):
    return ran in ('freddy', 'miku', 'caine', 'deepti', 'gem') and bool(CFG['projects'][project].get('repo'))

# ---------------------------------------------------------------- questions for Manuel (Telegram)
# Manuel 2026-10-25 (voice 2104): Talk notifications get lost. Everything that needs his decision is also queued
# for tools/manuel_ask.py (service manuel-ask), which sends it to his Telegram, with buttons when there are
# options, and routes his answer back (card comment / Talk relay / Muse click). Sources: agent Talk messages that
# mention @mrar1995, cards that get the Decide label (NEEDS_MANUEL, FAILED, by hand), card errors.

PNAME = {'hq': 'HQ', 'docreview': 'DocReview', 'lucidmail': 'LucidMail', 'paula': 'Paula', 'automusic': 'AutoMusic',
         'exmint': 'ExMint', 'muse': 'Muse'}
ASK_LOCK = threading.Lock()   # a dispatcher post and its ask register together, so poll_talk never asks twice


def ask_manuel(keys, title, body, link, route, options=None):
    try:
        if options is None:
            body, options = manuel_ask.parse_options(body)
        aid = manuel_ask.ask(keys, title, body, link, options, route)
        if aid:
            log.info('ask manuel P%s %s', aid, keys[0])
        return aid
    except Exception:
        log.error('ask_manuel failed\n%s', traceback.format_exc())


def card_url(board, cid):
    return f'{NC}/apps/deck/board/{board}/card/{cid}'


def room_url(room, mid=None):
    return f'{NC}/call/{room}' + (f'#message_{mid}' if mid else '')


def mentions_manuel(m):
    return '@mrar1995' in m.get('message', '') or any(
        isinstance(p, dict) and p.get('type') == 'user' and p.get('id') == 'mrar1995'
        for p in (m.get('messageParameters') or {}).values())


def mark_decide(cid, board):
    """Ask key for a card entering Decide, or None when this Decide spell was already asked
    (poll_deck's scan and deck_job share it; the spell ends when the label goes)."""
    cid = str(cid)
    with _lock:
        seen = STATE.setdefault('decide_seen', {})
        if cid in seen:
            return None
        gen = STATE.setdefault('decide_gen', {})
        gen[cid] = gen.get(cid, 0) + 1
        seen[cid] = [board, time.time()]
    save_state()
    return f'decide:{cid}:{gen[cid]}'


def deck_route(project, board, card, agent='ringo', mid=None):
    return {'type': 'deck', 'project': project, 'board': board, 'card': card['id'], 'title': card['title'],
            'room': CFG['projects'][project]['room'], 'agent': agent, 'mid': mid}


RETRY_OPTIONS = [{'label': 'Reintentar', 'action': 'requeue'}, {'label': 'Que Ringo lo revise', 'action': 'relay'}]


# ---------------------------------------------------------------- talk loop

POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix='job')


def talk_job(agent, project, room, m, depth, root=None, ack=False):
    mid = m['id']
    talk_react(agent, room, mid, '👀')
    text = posted = None
    try:
        task = f"{m['actorDisplayName']} wrote: {render(m)}"
        if ack:
            task += ('\n\n(This replies to your message. Act on it if it needs action; keep any reply short. '
                     'It goes back to them only if it asks a question.)')
        parent = (m.get('parent') or {}).get('id')
        ran, text, rec = execute(agent, project, task, talk_history(room),
                                 lambda note: talk_post('oscar', room, note, reply_to=mid, depth=depth),
                                 thread_key=f'msg-{parent}' if parent else None)
        rec = rec if isinstance(rec, dict) else None
        posted = talk_post(ran, room, text, reply_to=mid, depth=depth + 1, answer='ack' if ack else 'reply')
        task_note(root, ran, text, posted)
        if rec and posted:
            remember_thread([f'msg-{posted}', f'msg-{mid}'], rec)
        if UID2AGENT.get(m['actorId']) == 'ringo' and needs_review(project, ran):
            note = lambda n: talk_post('oscar', room, n, reply_to=posted or mid, depth=99)
            verdict, body, _, _ = review_loop(project, task, ran, text, f'msg-{mid}', note)
            task_note(root, 'ringo', f'Review: {verdict}. {body}', extra_job=True)
            if verdict == 'NO_CLAUDE':
                talk_post('oscar', room, 'No-Claude mode: Ringo is offline, no review performed. Freddy keeps '
                          'working; Ringo reviews these threads when Claude is back.', reply_to=posted or mid, depth=99)
            elif verdict == 'APPROVED':
                talk_react('ringo', room, posted or mid, '👍')   # silent approval
                if body.strip():
                    talk_post('ringo', room, body, reply_to=posted or mid, depth=99)
            elif verdict == 'NEEDS_MANUEL':
                talk_post('ringo', room, f'@mrar1995 decision needed: {body}', reply_to=posted or mid, depth=99)
            else:
                talk_post('ringo', room, body, reply_to=posted or mid, depth=99)
        talk_react(ran, room, mid, '✅')
    except ClaudeLimit as e:
        until = e.args[1]
        if claude_down() and agent in ('ringo', 'miku'):
            talk_post('oscar', room, f"{display(agent)} is offline (no-Claude mode, ON until switched back; "
                      f"weekly Claude reset expected {hhmm(until)}). Send implementation work to @freddy; Ringo "
                      'will review and integrate when Claude is back.', reply_to=mid, depth=99)
            log.info('talk msg=%s no-claude mode: %s offline answer', mid, agent)
            task_note(root, 'oscar', f'No-Claude mode: {display(agent)} offline.')
        elif text is None:   # no reply yet: rerun the message after the reset
            with _lock:
                STATE.setdefault('deferred_talk', []).append(
                    {'agent': agent, 'project': project, 'room': room, 'm': m, 'depth': depth, 'ts': time.time()})
            save_state()
            talk_post('oscar', room, f'{display(agent)} is at the Claude session limit; I will rerun this message '
                      f'at about {hhmm(until)}.', reply_to=mid, depth=99)
            log.info('talk msg=%s for %s deferred until %s', mid, agent, hhmm(until))
        else:              # the reply is posted; only Ringo's review is lost
            talk_post('oscar', room, f"Ringo's review of this reply was skipped: Claude session limit until {hhmm(until)}. "
                      'Ask him again after that if it needs one.', reply_to=posted or mid, depth=99)
            log.info('talk msg=%s review skipped (claude limit)', mid)
        task_note(root, 'oscar', f'Held by the Claude session limit until {hhmm(until)}.')
    except Exception:
        log.error('talk_job failed\n%s', traceback.format_exc())
        talk_post('oscar', room, f'{agent} hit an error on this one; details in the dispatcher log.', reply_to=mid)
        task_note(root, 'oscar', f'{agent} hit an error; details in the dispatcher log.')
    finally:
        task_job_end(root)


def poll_talk():
    for project, p in CFG['projects'].items():
        room = p['room']; last = STATE['last_id'].get(room)
        if last is None:  # first run: start from now, do not replay history
            r = SESS['oscar'].get(f'{TALK}/chat/{room}', params={'lookIntoFuture': 0, 'limit': 1,
                                                                 'setReadMarker': 0}, timeout=30)
            if r.status_code == 200 and r.json()['ocs']['data']:
                STATE['last_id'][room] = r.json()['ocs']['data'][0]['id']; save_state()
            continue
        r = SESS['oscar'].get(f'{TALK}/chat/{room}', params={'lookIntoFuture': 1, 'timeout': 0,
                              'lastKnownMessageId': last, 'limit': 100, 'setReadMarker': 0}, timeout=30)
        if r.status_code == 304:
            continue
        if r.status_code != 200:
            log.warning('poll %s -> %s', room, r.status_code); continue
        for m in r.json()['ocs']['data']:
            STATE['last_id'][room] = max(STATE['last_id'][room], m['id'])
            if m.get('messageType') != 'comment' or m.get('actorType') != 'users':
                continue
            author = UID2AGENT.get(m['actorId'])
            if author == 'oscar' and render(m).startswith(manuel_ask.RELAY_PREFIX):
                author = None   # Manuel's Telegram answer relayed by manuel-ask: route it like his own message
            elif author and mentions_manuel(m):
                with ASK_LOCK:
                    ask_manuel([f"talk:{m['id']}"], f"{PNAME.get(project, project)} · {display(author)} te pregunta",
                               render(m), room_url(room, m['id']),
                               {'type': 'talk', 'project': project, 'room': room, 'mid': m['id'], 'agent': author})
            sm = STOP_RE.match(render(m))
            if sm and author in (None, 'oscar'):
                handoff = (sm.group(1) or sm.group(2)).lower() == 'stop'
                ok = stop_freddy(m['actorDisplayName'], handoff)
                talk_post('oscar', room, ('Stopping Freddy now.' + (' Miku will pick up his task.' if handoff else ''))
                          if ok else 'Freddy is not running anything right now.', reply_to=m['id'])
                continue
            depth = STATE['depth'].get(str(m['id']), 1 if author else 0)
            targets = targets_of(m) - {author}
            if author and 'oscar' in targets and '?' not in render(m):
                targets.discard('oscar')   # report-backs to Oscar need no reply
            ack = False
            if not targets:
                via = reply_target(m, author)
                if via:
                    targets = {via}; ack = bool(author) and '?' not in render(m)
            if not targets and not author and p.get('default_agent'):
                targets = {p['default_agent']}
            if not targets and author and p.get('agent_responder', {}).get(author):
                targets = {p['agent_responder'][author]}   # e.g. Muse room: Oscar answers Muse's posts
            root = None if ack else TASK_OF.get(m['id'])   # delivering an answer is no new task step
            if (root is None and not ack and targets and depth < CFG['max_chain_depth'] and m['actorId'] in REQUESTERS
                    and str(m['id']) not in STATE['depth']):   # a new ask, not a dispatcher-posted reply
                root = m['id']; task_open(root, project, room, m)
            for agent in targets:
                if depth >= CFG['max_chain_depth']:
                    log.info('chain depth limit hit msg=%s', m['id']); continue
                log.info('talk -> %s project=%s msg=%s depth=%s', agent, project, m['id'], depth)
                task_job_start(root)
                POOL.submit(talk_job, agent, project, room, m, depth, root, ack)
        save_state()

# ---------------------------------------------------------------- deck loop

def deck(agent, method, path, **kw):
    r = SESS[agent].request(method, DECK + path, timeout=30, **kw)
    r.raise_for_status()
    return r.json() if r.text else None


def deck_comment(agent, card_id, text):
    if len(text) > DECK_COMMENT_MAX:
        text = text[:DECK_COMMENT_MAX] + ' [...] (full report in the project Talk room)'
    r = SESS[agent].post(f'{DECK_OCS}/cards/{card_id}/comments', json={'message': text}, timeout=30)
    if r.status_code >= 300:  # agent lacks board access: fall back to Oscar
        SESS['oscar'].post(f'{DECK_OCS}/cards/{card_id}/comments',
                           json={'message': f'[{agent}] {text}'[:1000]}, timeout=30)


def ensure_stack(board, title):
    """Return the id of stack `title` on board `board`, creating it when missing.
    Uses ringo session, except boards 27/30 need oscar (TASK2)."""
    bid = int(board)
    agent = 'oscar' if bid in (27, 30) else 'ringo'
    call = functools.partial(deck, agent)
    return stacks.ensure_stack(call, bid, title)


def ensure_label(board, title, color='31CC72'):
    """Ensure Deck label exists on board, creating it if missing."""
    bid = int(board)
    try:
        b = deck('oscar', 'GET', f'/boards/{bid}')
        labels_list = b.get('labels', []) if isinstance(b, dict) else []
        blabels = {l['title']: l['id'] for l in labels_list if isinstance(l, dict) and 'title' in l and 'id' in l}
        if title in blabels:
            return blabels[title]
        created = deck('oscar', 'POST', f'/boards/{bid}/labels', json={'title': title, 'color': color})
        if isinstance(created, dict) and 'id' in created:
            return created['id']
    except Exception as e:
        log.warning('ensure_label %r on board %s failed: %s', title, bid, e)
    return None


def escalate_card_to_argon(project, board, stacks, card, failure_notes, agent):
    cid = str(card['id'])
    bid = int(board)
    session_agent = 'oscar' if bid in (27, 30) else 'ringo'
    to_spec_id = stacks.get('To spec')
    if not to_spec_id:
        try:
            to_spec_id = ensure_stack(board, 'To spec')
        except Exception:
            pass
    doing = stacks.get('Doing')
    cpath = f"/boards/{board}/stacks/{card.get('stackId') or doing}/cards/{card['id']}"

    ensure_label(board, 'Argon')
    b = deck('oscar', 'GET', f'/boards/{board}')
    labels_list = b.get('labels', []) if isinstance(b, dict) else []
    blabels = {l['title']: l['id'] for l in labels_list if isinstance(l, dict) and 'title' in l and 'id' in l}
    if 'Argon' in blabels:
        deck('oscar', 'PUT', f"{cpath}/assignLabel", json={'labelId': blabels['Argon']})

    prev_label = display(agent)
    if prev_label in blabels:
        try:
            deck('oscar', 'PUT', f"{cpath}/removeLabel", json={'labelId': blabels[prev_label]})
        except Exception:
            pass

    deck_comment(session_agent, card['id'], f"[Escalated to Argon]\n{failure_notes}")

    if to_spec_id:
        deck(session_agent, 'PUT', f"{cpath}/reorder", json={'order': 0, 'stackId': to_spec_id})
    if scrum_enabled():
        with _lock:
            STATE.setdefault('escalation_log', []).append(
                {'cid': cid, 'title': card.get('title') or '', 'project': project, 'board': board,
                 'agent': agent, 'reason': str(failure_notes or '')[:300], 'at': time.time()})
            _prune_logs(STATE)
        save_state()
        note_card_return(project, board, stacks, card, failure_notes, agent, already_to_spec=True)
    log.info('card %s escalated to Argon in To spec: %s', cid, str(failure_notes)[:120])


def deck_job(agent, project, board, stacks, card):
    key = str(card['id'])
    held = False
    claim = claims().get(key)
    heartbeat = None
    if claim:
        set_claim_context(key)
        note_claim_activity(key)
        heartbeat = start_claim_heartbeat(key, claim.get('worker') or agent, claim.get('project') or project)
    try:
        doing = stacks.get('Doing')
        if doing:
            deck('oscar', 'PUT', f"/boards/{board}/stacks/{card['stackId']}/cards/{card['id']}/reorder",
                 json={'order': 0, 'stackId': doing})
        r = SESS['oscar'].get(f"{DECK_OCS}/cards/{card['id']}/comments", params={'limit': 10}, timeout=30)
        comments = r.json()['ocs']['data'] if r.status_code == 200 else []
        ctx = '\n'.join(f"{c['actorDisplayName']}: {c['message']}" for c in reversed(comments))
        # A returned card keeps its "Returned to To Do" line on top of the description; put it first in the
        # prompt too, so the worker reviews the earlier progress before starting (TASK3).
        returned = top_returned_note(card.get('description'))
        task = (f"Deck card #{card['id']} on board {board}: {card['title']}\n\n"
                + (f"QUEUE NOTE: {returned}\n\n" if returned else '')
                + f"{card.get('description') or '(no description)'}\n\n"
                'When done, summarise what you did and how you verified it.')
        room = CFG['projects'][project]['room']
        try:
            ran, text, flag = execute(agent, project, task, ctx or '(no comments)',
                                      lambda note: deck_comment('oscar', card['id'], note),
                                      thread_key=f"card-{card['id']}")
        except ClaudeLimit as e:   # nothing done yet: back to To do, picked up after the reset (poll_deck waits)
            ran, text, flag = 'oscar', f'{display(agent)} hit the Claude session limit; requeued, retried after {hhmm(e.args[1])}.', 'REQUEUE'
            deck_comment('oscar', card['id'], text)
        if flag == 'REQUEUE':   # Freddy busy/paused: back to To do with its label, picked up again later
            if text.startswith('Freddy paused this card'):
                deck_comment('oscar', card['id'], text)
            todo = stacks.get('To do')
            if todo and doing:
                deck('oscar', 'PUT', f"/boards/{board}/stacks/{doing}/cards/{card['id']}/reorder",
                     json={'order': 0, 'stackId': todo})
            log.info('card %s requeued: %s', card['id'], text[:80])
            return
        deck_comment(ran, card['id'], text)
        title = f"Card #{card['id']} \"{card['title']}\""
        if CFG.get('specialists'):
            is_timeout = 'timed out' in (text or '').lower() or 'timeout' in (text or '').lower()
            is_complex = 'unexpected complexity' in (text or '').lower()
            is_deploy = 'deploy fail' in (text or '').lower() or 'deploy failed' in (text or '').lower()
            if flag == 'FAILED':
                fail_count = STATE.setdefault('card_failures', {}).get(str(card['id']), 0) + 1
                STATE['card_failures'][str(card['id'])] = fail_count
                save_state()
            else:
                fail_count = STATE.get('card_failures', {}).get(str(card['id']), 0)

            if is_timeout or is_complex or is_deploy or (flag == 'FAILED' and fail_count >= 2):
                escalate_card_to_argon(project, board, stacks, card, text, agent)
                return
            elif flag != 'FAILED':
                if str(card['id']) in STATE.get('card_failures', {}):
                    STATE['card_failures'].pop(str(card['id']), None)
                    save_state()

        if flag == 'FAILED':    # worker failure: return the card to To do, NEVER a Decide/Telegram ask (TASK3)
            failed = ran if ran in CFG['agents'] else agent
            reason = f'{display(failed)} failed on this card: {(text or "(no output)")[:500]}'
            talk_post('oscar', room, f'{title}: {display(failed)} failed; returning the card to To do '
                      '(worker failures never go to Decide).', depth=99)
            _append_scrum_event('failure_log', {'cid': key, 'worker': failed, 'project': project,
                                                'kind': 'failure', 'reason': reason[:300],
                                                'at': time.time()})
            worker_failure_return(failed, project, board, stacks, card, reason)
            return
        # The coder run is over: stop its claim before the review, which may take longer than stall_minutes
        # (the card legitimately stays in Doing until Ringo answers).
        if heartbeat:
            heartbeat.stop(); heartbeat = None
        if claim:
            release_claim(key); claim = None
        set_claim_context(None)
        held = finish_card(agent, project, board, stacks, card, task, ran, text)
    except Exception as e:
        log.error('deck_job failed\n%s', traceback.format_exc())
        if CFG.get('specialists'):
            err_msg = str(e)
            is_timeout = 'timed out' in err_msg.lower() or 'timeout' in err_msg.lower()
            fail_count = STATE.setdefault('card_failures', {}).get(str(card['id']), 0) + 1
            STATE['card_failures'][str(card['id'])] = fail_count
            save_state()
            _append_scrum_event('failure_log', {'cid': key, 'worker': agent, 'project': project,
                                                'kind': 'timeout' if is_timeout else 'error',
                                                'reason': err_msg[:300], 'at': time.time()})
            if is_timeout or fail_count >= 2:
                try:
                    escalate_card_to_argon(project, board, stacks, card, f'Exception: {e}', agent)
                    return
                except Exception:
                    pass
        deck_error(agent, project, board, card, stacks=stacks)
    finally:
        if heartbeat:
            heartbeat.stop()
        set_claim_context(None)
        if claim:
            release_claim(key)
        if not held:
            with _lock:
                STATE['deck_active'].pop(key, None)
            save_state()


# ---------------------------------------------------------------- deck @mentions
# Manuel 2026-10-30: an @mention in a Deck card comment works like a Talk message, and the answer goes
# back to the card as a comment. Source: Oscar's Deck activity feed (he is on every project board).
# Only Manuel's comments trigger, so agents cannot ping each other in loops.
DECK_MENTION_USERS = set(CFG.get('deck_mention_users', ['mrar1995']))
ACTIVITY_DECK = NC + '/ocs/v2.php/apps/activity/api/v2/activity/deck'
BOARD2PROJECT = {b: name for name, p in CFG['projects'].items() for b in p['boards']}


def poll_deck_mentions():
    r = SESS['oscar'].get(ACTIVITY_DECK, params={'limit': 100}, timeout=30)
    if r.status_code != 200:
        log.warning('deck activity -> %s', r.status_code); return
    acts = sorted(r.json()['ocs']['data'], key=lambda a: a['activity_id'])
    last = STATE.get('deck_mention_last')
    if acts:
        STATE['deck_mention_last'] = max(last or 0, acts[-1]['activity_id'])
    if last is None:   # first run: start from now, do not replay old comments
        save_state(); return
    for a in acts:
        if a['activity_id'] <= last or a.get('type') != 'deck_comment' or a.get('user') not in DECK_MENTION_USERS:
            continue
        rich = a['subject_rich'][1] if len(a.get('subject_rich') or []) > 1 else {}
        try:
            board, stack, cid = int(rich['board']['id']), int(rich['stack']['id']), int(rich['card']['id'])
        except (KeyError, TypeError, ValueError):
            continue
        project = BOARD2PROJECT.get(board)
        text = a.get('message') or rich.get('comment', {}).get('name', '')
        targets = {HANDLES[h.lower()] for h in MENTION_RE.findall(text)} - {'muse', 'vicky'}
        if not project or not targets:
            continue
        allow = frozen_allow()
        if allow is not None and str(cid) not in allow:
            log.info('frozen: deck mention skipped card=%s activity=%s', cid, a['activity_id'])
            deck_comment('oscar', cid, 'The company is frozen while the environment is being stabilised '
                         '(Manuel 2026-11-01), so this mention did not start any work. Repost it after the freeze.')
            continue
        for agent in targets:
            log.info('deck mention -> %s project=%s card=%s activity=%s', agent, project, cid, a['activity_id'])
            POOL.submit(deck_mention_job, agent, project, board, stack, cid, a['user'], text)
    save_state()


def deck_mention_job(agent, project, board, stack, cid, user, text):
    room = CFG['projects'][project]['room']
    try:
        card = deck('oscar', 'GET', f'/boards/{board}/stacks/{stack}/cards/{cid}')
        r = SESS['oscar'].get(f'{DECK_OCS}/cards/{cid}/comments', params={'limit': 10}, timeout=30)
        comments = r.json()['ocs']['data'] if r.status_code == 200 else []
        ctx = '\n'.join(f"{c['actorDisplayName']}: {c['message']}" for c in reversed(comments))
        who = 'Manuel' if user == 'mrar1995' else user
        task = (f"{who} wrote in a comment on Deck card #{cid} ({card['title']}, board {board}): {text}\n\n"
                f"Card description:\n{card.get('description') or '(no description)'}\n\n"
                'Your answer is posted as a comment on this card, so keep it short and self-contained '
                '(under 900 characters when you can). Do what is asked; if it changes the plan, update the card.')
        try:
            ran, out, _flag = execute(agent, project, task, ctx or '(no comments)',
                                      lambda note: deck_comment('oscar', cid, note), thread_key=f'card-{cid}')
        except ClaudeLimit as e:
            ran, out = 'oscar', (f'{display(agent)} is at the Claude session limit until {hhmm(e.args[1])}. '
                                 'Mention him again after that.')
        deck_comment(ran, cid, out)
        talk_post('oscar', room, f"{display(ran)} answered {who} on card #{cid} «{card['title']}»: {card_url(board, cid)}"
                  + (f'\n\nFull answer:\n{out}' if len(out) > DECK_COMMENT_MAX else ''), depth=99)
    except Exception:
        log.error('deck_mention_job failed\n%s', traceback.format_exc())
        deck_comment('oscar', cid, f'{display(agent)} hit an error on this mention; details in the dispatcher log.')


def deck_error(agent, project, board, card, stacks=None):
    """A runner/dispatcher error on a card is a worker failure: comment and retry, never ask Manuel (TASK3)."""
    text = f'{display(agent)} hit an error on this card; see the dispatcher log.'
    deck_comment('oscar', card['id'], text)
    returned = False
    if stacks and str(card['id']) in claims():
        returned = worker_failure_return(agent, project, board, stacks, card,
                                         f'{display(agent)} hit an error (traceback in dispatcher.log).')
    talk_post('oscar', CFG['projects'][project]['room'],
              f"Card #{card['id']} \"{card['title']}\": {display(agent)} hit an error; see the dispatcher log."
              + (' It was returned to To do.' if returned else ''), depth=99)


def finish_card(agent, project, board, stacks, card, task, ran, text):
    """Review (if needed), label and report a card whose coder run is done. Returns True when the review is held
    by the Claude limit: the card stays in Doing and in deck_active, and resume_reviews reruns this after the reset."""
    doing = stacks.get('Doing')
    room = CFG['projects'][project]['room']
    title = f"Card #{card['id']} \"{card['title']}\""
    labels = {l['title']: l['id'] for l in deck('oscar', 'GET', f'/boards/{board}')['labels']}
    cpath = f"/boards/{board}/stacks/{doing or card['stackId']}/cards/{card['id']}"
    final_label = 'To review'
    if needs_review(project, ran):
        try:
            verdict, body, _, _ = review_loop(project, task, ran, text, f"card-{card['id']}",
                                              lambda n: deck_comment('oscar', card['id'], n))
        except ClaudeLimit as e:
            key = str(card['id'])
            first = key not in STATE.get('pending_reviews', {})
            with _lock:
                STATE.setdefault('pending_reviews', {})[key] = {
                    'agent': agent, 'project': project, 'board': board, 'stacks': stacks, 'card': card,
                    'task': task, 'ran': ran, 'text': text, 'ts': time.time()}
            save_state()
            if first:
                deck_comment('oscar', card['id'], f"Ringo's review is held: Claude session limit until {hhmm(e.args[1])}. "
                             'The card stays in Doing and the review reruns by itself after that.')
            log.info('card %s review held until %s', key, hhmm(e.args[1]))
            return True
        if verdict == 'FAILED':   # Ringo's review run crashed: a worker failure, requeue; never a Decide (TASK3)
            verdict, body = 'REQUEUE', (f"Ringo's review run failed (worker unavailability), so this card is "
                                        f"not reviewed. {body}")
        if verdict == 'NO_CLAUDE':
            # No-Claude mode (Manuel 2026-10-27): the pipeline does not stop. The card advances to To review
            # with an explicit note; Ringo reviews AND integrates it when Claude is back. Nothing merges before that.
            deck_comment('oscar', card['id'], f'NO_CLAUDE: {body}')
            talk_post('oscar', room, f'{title}: no-Claude mode, no review done. The card waits in To review: '
                      'Ringo reviews and integrates it when the subscription is back.', depth=99)
            log.info('card %s NO_CLAUDE: review and integration deferred to Ringo', card['id'])
            if final_label in labels:
                deck('oscar', 'PUT', cpath + '/assignLabel', json={'labelId': labels[final_label]})
            lab = display(agent)
            if lab in labels:
                deck('oscar', 'PUT', cpath + '/removeLabel', json={'labelId': lab})
            return False
        if verdict == 'REQUEUE':
            deck_comment('ringo', card['id'], 'FIXES (coder was busy, card requeued; continue from the work in the repo):\n' + body)
            todo = stacks.get('To do')
            if todo:
                deck('oscar', 'PUT', f"{cpath}/reorder", json={'order': 0, 'stackId': todo})
            log.info('card %s requeued with review fixes', card['id'])
            return False
        deck_comment('ringo', card['id'], f'{verdict}: {body}' if body else verdict)
        if verdict == 'NEEDS_MANUEL':
            final_label = 'Decide'
            dkey = mark_decide(card['id'], board)
            with ASK_LOCK:
                mid = talk_post('ringo', room, f'@mrar1995 {title} needs a decision: {body}', depth=99)
                ask_manuel([k for k in (dkey, mid and f'talk:{mid}') if k],
                           f"{PNAME.get(project, project)} #{card['id']} «{card['title']}» · Ringo necesita una decisión",
                           body, card_url(board, card['id']), deck_route(project, board, card, mid=mid))
        else:
            talk_post('ringo', room, f'{title}: {verdict.lower().replace("_", " ")}. Ready for your review.\n'
                      + summarize(task, [(ran, text), ('ringo', f'Review: {verdict}. {body}')]), depth=99)
    else:
        talk_post(ran, room, f"{title}:\n\n{text}", depth=1)
    if CFG.get('specialists') and (verdict == 'APPROVED' or not needs_review(project, ran)):
        to_test = stacks.get('To test')
        if not to_test:
            try:
                to_test = ensure_stack(board, 'To test')
            except Exception:
                pass
        if to_test:
            session_agent = 'oscar' if int(board) in (27, 30) else 'ringo'
            deck(session_agent, 'PUT', f"{cpath}/reorder", json={'order': 0, 'stackId': to_test})
            log.info('card %s reviewed -> moved to To test', card['id'])
            lab = display(agent)
            if lab in labels:
                deck('oscar', 'PUT', cpath + '/removeLabel', json={'labelId': labels[lab]})
            return False
    if final_label in labels:
        deck('oscar', 'PUT', cpath + '/assignLabel', json={'labelId': labels[final_label]})
    lab = display(agent)
    if lab in labels:
        deck('oscar', 'PUT', cpath + '/removeLabel', json={'labelId': labels[lab]})
    return False


RESUME_RETRY_MAX = 12


def resume_review(key):
    """Rerun a held review once the Claude gate is open (card still in Doing, coder's work untouched)."""
    pr = STATE['pending_reviews'][key]
    held = False
    try:
        held = finish_card(pr['agent'], pr['project'], pr['board'], pr['stacks'], pr['card'], pr['task'], pr['ran'], pr['text'])
    except Exception:
        log.error('resume_review %s failed\n%s', key, traceback.format_exc())
        deck_error('ringo', pr['project'], pr['board'], pr['card'])
        # Live lesson 2026-11-09: a Nextcloud error during a resumed review used to DROP the
        # pending_reviews record, stranding the card in Doing with no review and no claim
        # (cards 323/377/378/414/500). Keep the hold so resume_held retries it on the next
        # open-gate poll; after RESUME_RETRY_MAX give up and let the watchdog see it.
        pr['resume_fails'] = pr.get('resume_fails', 0) + 1
        if pr['resume_fails'] < RESUME_RETRY_MAX:
            held = True
            log.warning('holding review card %s for retry (attempt %s/%s)', key, pr['resume_fails'], RESUME_RETRY_MAX)
    finally:
        if not held:
            with _lock:
                STATE['pending_reviews'].pop(key, None)
                STATE['deck_active'].pop(key, None)
            save_state()
        RESUMING.discard(key)


RESUMING = set()


def resume_held():
    """After the Claude limit resets: rerun held reviews and deferred Talk messages (older than a day are dropped)."""
    if claude_limited() or frozen_allow() is not None:
        return
    for key, pr in list((STATE.get('pending_reviews') or {}).items()):
        if key in RESUMING:
            continue
        RESUMING.add(key)
        with _lock:
            STATE['deck_active'][key] = f"{pr['agent']}@{pr['project']}"
        log.info('resume held review card=%s', key)
        POOL.submit(resume_review, key)
    with _lock:
        items, STATE['deferred_talk'] = STATE.get('deferred_talk') or [], []
    for it in items:
        if time.time() - it['ts'] > 86400:
            log.info('deferred talk msg=%s dropped (older than a day)', it['m']['id']); continue
        log.info('resume deferred talk -> %s msg=%s', it['agent'], it['m']['id'])
        POOL.submit(talk_job, it['agent'], it['project'], it['room'], it['m'], it['depth'], None)
    save_state()


FREEZE_FLAG = f'{HOME}/frozen'   # Manuel 2026-11-01: while it exists no card, nudge, held review or Deck mention starts work


def frozen_allow():
    """None when not frozen, else the card ids (strings) in the flag file that may still start (agent tests)."""
    try:
        with open(FREEZE_FLAG) as f:
            return {w for line in f if not line.startswith('#') for w in line.split()}
    except FileNotFoundError:
        return None


def freddy_ready():
    if os.path.exists(PAUSE_FLAG):
        return False, 'Freddy/Brain paused by Manuel'
    if FREDDY_LOCK.locked():
        return False, 'Freddy is already busy with another task'
    return brain_available()


def card_assignee_agent(card):
    """The first non-Hermes agent whose Nextcloud user is assigned to the card, else None."""
    uids = {(u.get('participant') or {}).get('uid') for u in card.get('assignedUsers') or []}
    return next((n for n, a in CFG['agents'].items() if n not in HERMES_AGENTS and a.get('nc_user') in uids), None)


def relabel_card_to_freddy(board, card, from_title):
    """No-Claude mode: hand a Ringo/Miku card to Freddy (swap the labels, explain on the card). True on success."""
    try:
        card_labels = {l['title'] for l in card.get('labels') or []}
        if 'Freddy' in card_labels:      # already handed over in an earlier poll
            return True
        path = f"/boards/{board}/stacks/{card['stackId']}/cards/{card['id']}"
        blabels = {l['title']: l['id'] for l in deck('oscar', 'GET', f'/boards/{board}')['labels']}
        if from_title in card_labels and from_title in blabels:
            deck('oscar', 'PUT', path + '/removeLabel', json={'labelId': blabels[from_title]})
        if 'Freddy' in blabels:
            deck('oscar', 'PUT', path + '/assignLabel', json={'labelId': blabels['Freddy']})
        deck_comment('oscar', card['id'], f"{from_title} is offline (no-Claude mode): the dispatcher handed this "
                       'card to Freddy. Ringo will review and integrate it when Claude returns.')
        return True
    except Exception:
        log.error('no-claude relabel card %s failed\n%s', card['id'], traceback.format_exc())
        return False


def label_card_for_pull(board, card, agent):
    try:
        stack_id = card.get('stackId')
        path = f"/boards/{board}/stacks/{stack_id}/cards/{card['id']}"
        b = deck('oscar', 'GET', f'/boards/{board}')
        labels_list = b.get('labels', []) if isinstance(b, dict) else []
        blabels = {l['title']: l['id'] for l in labels_list if isinstance(l, dict) and 'title' in l and 'id' in l}
        agent_label = display(agent)
        if agent_label in blabels:
            deck('oscar', 'PUT', path + '/assignLabel', json={'labelId': blabels[agent_label]})
        card.setdefault('labels', []).append({'title': agent_label, 'id': blabels.get(agent_label, 0)})
        return True
    except Exception:
        log.error('pull label card %s failed\n%s', card['id'], traceback.format_exc())
        return False


RETURNED_NOTE_RE = re.compile(r'^\[[^\]]+ Toronto\] Returned to To Do, due to worker unavailability')


def toronto_stamp(now=None):
    from zoneinfo import ZoneInfo
    return dt.datetime.fromtimestamp(now or time.time(), ZoneInfo('America/Toronto')).strftime('%Y-%m-%d %H:%M')


def returned_note(agent, now=None):
    """The line prepended to a card whose worker died; keeps the earlier such lines below it (newest first)."""
    return (f'[{toronto_stamp(now)} Toronto] Returned to To Do, due to worker unavailability '
            f'({display(agent)}). Before starting, review what is the current progress of this task '
            '(git log/branch, WIP commit) and continue from there.')


def top_returned_note(description):
    """The card's top 'Returned to To Do' line, or '' when the description does not start with one."""
    for line in (description or '').splitlines():
        line = line.strip()
        if line:
            return line if RETURNED_NOTE_RE.match(line) else ''
    return ''


def stall_seconds():
    return float(CFG.get('stall_minutes', 20)) * 60


def claim_stall_reason(claim, now=None):
    """Why a claimed card's worker is dead/unavailable, or None while it is alive and heartbeating."""
    now = time.time() if now is None else now
    worker = claim.get('worker')
    if not worker:
        return None
    until = agent_limited(worker)
    if until:
        return (f'{display(worker)} hit a quota/auth/limit error; the card is skipped until the limit '
                f'resets ({hhmm(until)}).')
    last = claim.get('last_activity') or claim.get('claimed_at') or 0
    pid = claim.get('pid')
    if pid and not pid_alive(pid):
        return f'{display(worker)} process is gone and it emitted no output or commit since {hhmm(last)}.'
    if now - last > stall_seconds():
        mins = int(stall_seconds() // 60)
        return f'no heartbeat from {display(worker)} for {mins} min (last activity {hhmm(last)}).'
    return None


def return_card_to_todo(cid, worker, project, board, reason, stacks, card):
    """Remove the worker label, snapshot its WIP, and put the card on top of To do with a note.
    `stacks` is {title: id}; `card` is the Deck card dict (updated in place for a same-tick re-pull)."""
    cid = str(cid)
    todo = stacks.get('To do')
    doing = stacks.get('Doing') or card.get('stackId')
    if not todo:
        raise RuntimeError(f'no To do stack on board {board}')
    desc = card.get('description') or ''
    note = returned_note(worker)
    card['description'] = note + (f'\n\n{desc}' if desc else '')
    card['stackId'] = todo
    keep = {display(worker), worker, worker.capitalize(), worker.title()}
    card['labels'] = [l for l in card.get('labels') or [] if l.get('title') not in keep]
    b = deck('oscar', 'GET', f'/boards/{board}')
    blabels = {l['title']: l['id'] for l in (b.get('labels', []) if isinstance(b, dict) else [])
               if isinstance(l, dict) and 'title' in l and 'id' in l}
    lab = display(worker)
    if lab in blabels:
        deck('oscar', 'PUT', f"/boards/{board}/stacks/{doing}/cards/{cid}/removeLabel",
             json={'labelId': blabels[lab]})
    wip = ''
    repo = (CFG['projects'].get(project) or {}).get('repo')
    if repo and worker != 'ringo':
        try:
            wip = snapshot_wip(repo, cid, worker)
        except Exception:
            log.warning('snapshot_wip for card %s failed\n%s', cid, traceback.format_exc())
    owner = card.get('owner')
    owner = owner.get('uid') if isinstance(owner, dict) else (owner or 'oscar')
    # Deck v1.0 card PUT is an all-or-nothing contract: the body must carry
    # title+type+owner+duedate alongside description, or the API answers 400
    # and the whole return aborts before reorder/comment (measured live on
    # Nextcloud 34, 2026-11-09: a description-only PUT 400s on every card).
    deck('oscar', 'PUT', f"/boards/{board}/stacks/{doing}/cards/{cid}",
         json={'description': card['description'], 'title': card['title'],
               'type': card.get('type') or 'plain', 'owner': owner,
               'duedate': card.get('duedate')})
    deck('oscar', 'PUT', f"/boards/{board}/stacks/{doing}/cards/{cid}/reorder",
         json={'order': 0, 'stackId': todo})
    deck_comment('oscar', cid, f'Returned to To do: {reason}' + (f' {wip}' if wip else ''))
    with _lock:
        claims().pop(cid, None)
        STATE.get('deck_active', {}).pop(cid, None)
        if scrum_enabled():
            STATE.setdefault('return_log', []).append(
                {'cid': cid, 'title': card.get('title') or '', 'worker': worker, 'project': project,
                 'board': board, 'reason': str(reason or '')[:300], 'at': time.time()})
            _prune_logs(STATE)
    save_state()
    if scrum_enabled():
        note_card_return(project, board, stacks, card, reason, worker)
    log.info('stalled: card %s returned to To do (%s)', cid, reason)
    return card


def check_stalled_claims(board_stacks):
    """Each pull tick: return cards whose claimed worker is gone, limited or silent. Cheap and local."""
    now = time.time()
    returned = []
    for cid, claim in list(claims().items()):
        try:
            reason = claim_stall_reason(claim, now)
            if not reason:
                continue
            board = claim.get('board')
            all_stacks = board_stacks.get((claim.get('project'), board))
            if all_stacks is None:
                continue
            doing = next((s for s in all_stacks if s.get('title') == 'Doing'), None)
            todo = next((s for s in all_stacks if s.get('title') == 'To do'), None)
            if not doing or not todo:
                continue
            card = next((c for c in doing.get('cards') or [] if str(c.get('id')) == str(cid)), None)
            if card is None:
                continue   # a human moved it: leave it alone
            labels = {l.get('title') for l in card.get('labels') or []}
            if display(claim['worker']) not in labels and claim['worker'] not in labels:
                continue   # a human relabelled it: leave it alone
            stacks = {s['title']: s['id'] for s in all_stacks}
            return_card_to_todo(cid, claim['worker'], claim.get('project'), board, reason, stacks, card)
            try:
                doing['cards'].remove(card)
            except ValueError:
                pass
            todo.setdefault('cards', []).insert(0, card)   # a second worker can pull it this same tick
            returned.append(str(cid))
        except Exception:
            log.error('returning stalled card %s failed\n%s', cid, traceback.format_exc())
    _sweep_orphan_doing(board_stacks, now)
    return returned


LEGACY_SWEEP_MAX = 3   # per tick: a healthy API must not be flooded by one cleanup pass


def _sweep_orphan_doing(board_stacks, now):
    """Return Doing cards that carry a coder-worker label but have no claim, no
    deck_active, no review hold and no recent activity: board state that predates
    the claim bookkeeping (found 2026-11-09: caine 484, gem 464, freddy 51/483 sat
    in Doing with zero live work and, with per_worker_doing=1, blocked their lanes
    permanently from pulling anything). Decide/Manuel/To review labels mark
    deliberate states and are left alone, as is any card a worker currently claims."""
    swept = 0
    for (project, board), all_stacks in (board_stacks or {}).items():
        if swept >= LEGACY_SWEEP_MAX:
            return
        doing = next((s for s in all_stacks if s.get('title') == 'Doing'), None)
        todo = next((s for s in all_stacks if s.get('title') == 'To do'), None)
        if not doing or not todo:
            continue
        stacks = {s['title']: s['id'] for s in all_stacks}
        for card in list(doing.get('cards') or []):
            if swept >= LEGACY_SWEEP_MAX:
                return
            cid = str(card.get('id'))
            labels = {str((l or {}).get('title') or '') for l in card.get('labels') or []}
            worker = _agent_from_labels(card)
            if not worker or worker not in PULL_WORKERS:
                continue
            if labels & EXCLUDED_PULL_LABELS:
                continue
            if cid in claims() or cid in (STATE.get('deck_active') or {}) \
                    or cid in (STATE.get('pending_reviews') or {}):
                continue     # live claim or review hold: the claim path owns this card
            lm = str(card.get('lastModified') or '')
            try:
                ts = dt.datetime.fromisoformat(lm.replace('Z', '+00:00')).timestamp()
                if now - ts < stall_seconds():
                    continue     # touched inside the stall window: a worker may be mid-flight
            except ValueError:
                pass             # no usable timestamp: old bookkeeping, sweep it
            try:
                return_card_to_todo(cid, worker, project, board,
                                    'Doing without a claim (board state predates claim bookkeeping)',
                                    stacks, card)
                doing['cards'].remove(card)
                todo.setdefault('cards', []).insert(0, card)
                swept += 1
            except Exception:
                log.error('legacy sweep of Doing card %s failed\n%s', cid, traceback.format_exc())


def worker_failure_return(agent, project, board, stacks, card, reason):
    """A worker failed: never a Decide/Telegram ask. Return the claimed card to To do, else just comment."""
    cid = str(card['id'])
    claim = claims().get(cid)
    if claim:
        try:
            return_card_to_todo(cid, claim.get('worker') or agent, project, board, reason, stacks, card)
            return True
        except Exception:
            log.error('returning failed card %s failed\n%s', cid, traceback.format_exc())
    deck_comment('oscar', cid, f'{reason} The card was not sent to Decide: worker failures are retried.')
    return False


BOARD_BACKOFF = {}   # board -> time of the next try after a 403/404 (board 28 logged 17k 403s, 2026-10-22..25)
PRIMARY_IDLE = {}    # primary project -> since when it has had no Freddy card ready or in flight


def in_flight():
    """agent@project of every card in flight, including cards a restart orphaned and not yet recovered."""
    return list(STATE['deck_active'].values()) + [r['who'] for r in STATE.get('recover', {}).values()]


def fetch_stacks(board):
    if time.time() < BOARD_BACKOFF.get(board, 0):
        return None
    try:
        stacks = deck('oscar', 'GET', f'/boards/{board}/stacks')
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (403, 404):
            if board not in BOARD_BACKOFF:
                log.warning('deck board %s: %s for oscar; retrying every 10 min, warning once', board, e.response.status_code)
            BOARD_BACKOFF[board] = time.time() + 600
            return None
        raise
    if BOARD_BACKOFF.pop(board, None):
        log.info('deck board %s: access back', board)
    return stacks


def get_free_specialist(project, is_escalated=False):
    """Return free specialist agent (Argon, Freddy [spec-only], or fallback Ringo), or None.
    Order: Argon, Freddy (spec-only), Ringo fallback. Freddy does NOT take escalations (TASK2 addendum)."""
    # 2026-11-09: while the shared Gem/Argon quota is out, To spec waits for Argon or a free Freddy.
    # No Ringo (Opus) fallback then: it looped on ClientA cards and spent 18 USD in one hour.
    # 2026-11-09 (Manuel's quota shield): Google gates are per MODEL. Argon runs the premium
    # gemini-4-argon model; when that quota dies ONLY Argon pauses, the flash worker lane (Gem/Caine)
    # stays alive and To spec descends the ladder instead of claiming a capped agent (card 523 wasted
    # a claim on an instant-reject run). While the flash (Gem) gate itself is closed, To spec waits
    # for a free Freddy and never burns Opus (ClientA 18 USD loop decision stays).
    if agy_limited() or agent_limited('argon'):
        if not is_escalated and not FREDDY_LOCK.locked() and not any(v.startswith('freddy@') for v in in_flight()):
            ok, _ = freddy_ready()
            if ok:
                return 'freddy'
        if not agy_limited() and not claude_limited() and not claude_down():
            ringo_lock = RINGO_LOCKS.get(project)
            if ringo_lock and not ringo_lock.locked():
                return 'ringo'
        return None
    # 1. Argon is the primary specialist
    if not ARGON_LOCK.locked():
        return 'argon'
    # 2. Freddy (when idle: no coder card, lock free) for un-escalated To spec cards
    if not is_escalated and not FREDDY_LOCK.locked() and not any(v.startswith('freddy@') for v in in_flight()):
        ok, _ = freddy_ready()
        if ok:
            return 'freddy'
    # 3. Ringo is the fallback specialist
    if not claude_limited() and not claude_down():
        ringo_lock = RINGO_LOCKS.get(project)
        if ringo_lock and not ringo_lock.locked():
            return 'ringo'
    return None


def specialist_spec_job(specialist, project, board, stacks, card):
    cid = str(card['id'])
    bid = int(board)
    session_agent = 'oscar' if bid in (27, 30) else 'ringo'
    to_spec_id = card.get('stackId') or stacks.get('To spec')
    todo_id = stacks.get('To do')

    try:
        task = (f"Review card #{card['id']} on board {board}: {card['title']}\n\n"
                f"{card.get('description') or '(no description)'}\n\n"
                "Write the solution for this card:\n"
                "- Approach\n"
                "- Files to modify or create\n"
                "- Acceptance test as numbered public steps between TEST-BRIEF and END-TEST-BRIEF markers with expected result\n"
                f"- Priority\n"
                f"- Project: {project}\n")

        ran, spec_text, flag = execute(specialist, project, task, card.get('description') or '',
                                       lambda note: deck_comment(session_agent, card['id'], note),
                                       thread_key=f"card-{card['id']}")

        orig_desc = card.get('description') or ''
        new_desc = (orig_desc.strip() + '\n\n' + spec_text.strip()).strip()
        # Deck v1.0 card PUT needs title+type+owner, else 400 (the 2026-11-09 loop: spec written, never saved).
        owner = card.get('owner')
        owner = owner.get('uid') if isinstance(owner, dict) else (owner or session_agent)
        deck(session_agent, 'PUT', f"/boards/{board}/stacks/{to_spec_id}/cards/{card['id']}",
             json={'description': new_desc, 'title': card['title'], 'type': card.get('type') or 'plain',
                   'owner': owner, 'duedate': card.get('duedate')})

        if scrum_enabled():
            check = dict(card)
            check['description'] = new_desc
            check['project'] = project
            ready, missing = is_ready(check)
            if not ready:
                deck_comment(session_agent, card['id'],
                             '[Definition of Ready] The spec is not ready for To do; the card stays in To spec. '
                             'Missing: ' + ', '.join(missing) + '.')
                STATE.setdefault('spec_failed', {})[cid] = time.time()   # no immediate re-spec loop
                log.info('DoR: card %s specced by %s but stays in To spec (missing %s)',
                         cid, ran, ', '.join(missing))
                return
        if todo_id:
            deck(session_agent, 'PUT', f"/boards/{board}/stacks/{to_spec_id}/cards/{card['id']}/reorder",
                 json={'order': 0, 'stackId': todo_id})
        log.info('spec: card %s specced by %s -> moved to To do', cid, ran)
    except Exception:
        log.error('specialist_spec_job failed for card %s\n%s', cid, traceback.format_exc())
        with _lock:  # never re-claim a card that just failed: 6 h cool-down
            STATE.setdefault('spec_failed', {})[cid] = time.time()
    finally:
        with _lock:
            STATE['deck_active'].pop(cid, None)
        save_state()


def specialist_code_job(specialist, project, board, stacks, card):
    cid = str(card['id'])
    bid = int(board)
    session_agent = 'oscar' if bid in (27, 30) else 'ringo'
    to_spec_id = card.get('stackId') or stacks.get('To spec')
    todo_id = stacks.get('To do')
    to_review_id = stacks.get('To review')
    cpath = f"/boards/{board}/stacks/{to_spec_id}/cards/{card['id']}"

    try:
        task = (f"Escalated card #{card['id']} on board {board}: {card['title']}\n\n"
                f"{card.get('description') or '(no description)'}\n\n"
                "Solve and implement this card. Report what you changed and how you verified it.")

        ran, code_text, flag = execute(specialist, project, task, '(escalated card)',
                                       lambda note: deck_comment(session_agent, card['id'], note),
                                       thread_key=f"card-{card['id']}")
        deck_comment(session_agent, card['id'], f"[Specialist {ran} solution]\n{code_text}")

        b = deck('oscar', 'GET', f'/boards/{board}')
        labels_list = b.get('labels', []) if isinstance(b, dict) else []
        blabels = {l['title']: l['id'] for l in labels_list if isinstance(l, dict) and 'title' in l and 'id' in l}

        if flag != 'FAILED' and to_review_id:
            deck(session_agent, 'PUT', f"{cpath}/reorder", json={'order': 0, 'stackId': to_review_id})
            if 'Argon' in blabels:
                deck('oscar', 'PUT', f"{cpath}/removeLabel", json={'labelId': blabels['Argon']})
            log.info('escalated card %s solved by %s -> To review', cid, ran)
        else:
            if 'Argon' in blabels:
                deck('oscar', 'PUT', f"{cpath}/removeLabel", json={'labelId': blabels['Argon']})
            if todo_id:
                deck(session_agent, 'PUT', f"{cpath}/reorder", json={'order': 0, 'stackId': todo_id})
            log.info('escalated card %s returned to pull queue by %s -> To do', cid, ran)
    except Exception:
        log.error('specialist_code_job failed for card %s\n%s', cid, traceback.format_exc())
    finally:
        with _lock:
            STATE['deck_active'].pop(cid, None)
        save_state()


def poll_to_spec(board_stacks):
    if not CFG.get('specialists'):
        return

    spec_started = False
    for project, p in CFG['projects'].items():
        if spec_started:
            break
        for board in p.get('boards', []):
            all_stacks = board_stacks.get((project, board))
            if not all_stacks:
                continue
            to_spec_stack = next((s for s in all_stacks if s.get('title') == 'To spec'), None)
            if not to_spec_stack or not to_spec_stack.get('cards'):
                continue

            stacks_map = {s['title']: s['id'] for s in all_stacks}

            for c in to_spec_stack.get('cards') or []:
                cid = str(c['id'])
                if cid in STATE.get('deck_active', {}):
                    continue
                if time.time() - (STATE.get('spec_failed') or {}).get(cid, 0) < 6 * 3600:
                    continue
                card_labels = {l['title'] for l in c.get('labels') or []}
                if card_labels & {'Manuel', 'Decide', 'Action needed'}:
                    continue

                is_escalated = 'Argon' in card_labels
                specialist = get_free_specialist(project, is_escalated=is_escalated)
                if not specialist:
                    return

                c['stackId'] = c.get('stackId') or to_spec_stack['id']

                with _lock:
                    STATE.setdefault('deck_active', {})[cid] = f'{specialist}@{project}'
                save_state()

                if is_escalated:
                    log.info('to_spec: escalated card %s taken by specialist %s on %s', cid, specialist, project)
                    POOL.submit(specialist_code_job, specialist, project, board, stacks_map, c)
                else:
                    log.info('to_spec: card %s taken for spec by %s on %s', cid, specialist, project)
                    POOL.submit(specialist_spec_job, specialist, project, board, stacks_map, c)

                spec_started = True
                break


def muse_test_job(project, board, stacks, card, muse_msg):
    cid = str(card['id'])
    bid = int(board)
    session_agent = 'oscar' if bid in (27, 30) else 'ringo'
    cpath = f"/boards/{board}/stacks/{card.get('stackId') or stacks.get('To test')}/cards/{card['id']}"
    todo_id = stacks.get('To do')
    done_id = stacks.get('Done') or stacks.get('Finished')
    to_spec_id = stacks.get('To spec')

    try:
        agent_ran, out = run_muse('muse', muse_msg)
        is_fail = ('fail' in out.lower() or agent_ran == 'oscar' or '[muse tool:' in out)

        if not is_fail:
            deck_comment(session_agent, card['id'], f"Muse test passed:\n{out[:500]}")
            if scrum_enabled():
                move_card_to_done(project, board, stacks, card, {'muse_passed': True},
                                  session_agent=session_agent)
            elif done_id:
                deck(session_agent, 'PUT', f"{cpath}/reorder", json={'order': 0, 'stackId': done_id})
            log.info('muse pass: card %s -> Done', cid)
        else:
            deck_comment(session_agent, card['id'], f"Muse test failed:\n{out[:1000]}")
            if todo_id:
                deck(session_agent, 'PUT', f"{cpath}/reorder", json={'order': 0, 'stackId': todo_id})
            log.info('muse fail: card %s -> To do', cid)

            finding = muse_feed.sanitize_brief(out)
            new_title = f"Muse test failure: {card['title']}"
            new_desc = (f"Finding from Muse test on #{card['id']} ({card['title']}):\n\n"
                        f"{finding}\n\n"
                        f"Original card: #{card['id']}")
            if to_spec_id:
                deck(session_agent, 'POST', f"/boards/{board}/stacks/{to_spec_id}/cards",
                     json={'title': new_title, 'description': new_desc, 'order': 0})
                log.info('muse fail: created new card in To spec for %s', cid)
    except Exception:
        log.error('muse_test_job failed for card %s\n%s', cid, traceback.format_exc())
    finally:
        with _lock:
            STATE['deck_active'].pop(cid, None)
        save_state()


NO_VISIBLE = '\0NO_VISIBLE_EFFECT'
_NO_VISIBLE_SEEN = set()   # cards already judged; a blocked move must not re-run Haiku every poll


def auto_test_brief(card, pcfg, project):
    """Fallback TEST-BRIEF for a To test card whose spec had none. Haiku sees the card text only (never code) and
    writes numbered steps a stranger could do on the PUBLIC site, plus an expected result. None on any failure."""
    target = (CFG.get('muse_targets') or {}).get(project) or ''
    hosts = ', '.join(pcfg.get('muse_hosts') or []) or target
    prompt = ("Write a black-box test brief for an external tester who only sees the public website, nothing else.\n"
              f"Public site(s): {hosts}\n"
              "Rules: 3-8 numbered steps a visitor can do in a browser; then a line 'Expected result: ...'. "
              "Describe what a visitor sees or does. NO code, file paths, commands, server names, credentials, "
              "emails, client names or personal data. FIRST decide: if the card has NOTHING a visitor or end user could "
              "see or notice (server, mail, backup, tooling, refactor, docs, internal config), output exactly the single "
              "word NO_VISIBLE_EFFECT and nothing else. Otherwise output only the brief.\n\n"
              f"Card title: {card.get('title','')}\nCard text:\n{(card.get('description') or '')[:3000]}")
    try:
        if claude_limited() or claude_down():
            return None
        os.makedirs(f'{HOME}/work/_scratch', exist_ok=True)
        rc, out, _ = run_cmd(['claude', '-p', '--model', 'haiku', '--output-format', 'json'],
                             f'{HOME}/work/_scratch', 300, stdin=prompt)
        ev = json.loads(out)
        text = (ev.get('result') or '').strip() if rc == 0 and not ev.get('is_error') else ''
        if text.strip().upper().startswith('NO_VISIBLE_EFFECT'):
            return NO_VISIBLE
        if not text or 'expected result' not in text.lower():
            return None
        deck_comment('oscar', card['id'], 'No TEST-BRIEF on this card; auto-written brief sent to Muse:\n' + text[:1500])
        return text
    except Exception as e:
        log.warning('auto_test_brief failed for card %s: %s', card.get('id'), e)
        return None


def poll_to_test(board_stacks):
    if not CFG.get('specialists'):
        return

    for project, p in CFG['projects'].items():
        for board in p.get('boards', []):
            all_stacks = board_stacks.get((project, board))
            if not all_stacks:
                continue
            to_test_stack = next((s for s in all_stacks if s.get('title') == 'To test'), None)
            if not to_test_stack or not to_test_stack.get('cards'):
                continue

            stacks_map = {s['title']: s['id'] for s in all_stacks}

            for c in to_test_stack.get('cards') or []:
                cid = str(c['id'])
                if cid in STATE.get('deck_active', {}):
                    continue

                desc = c.get('description') or ''
                brief = muse_feed.extract_test_brief(desc)
                if brief is None:
                    # Manuel 2026-11-09: Muse tests EVERY completed card. No brief from the spec -> Haiku writes a
                    # public-behaviour one from the card text; the sanitizer below still strips code/paths/URLs.
                    if cid in _NO_VISIBLE_SEEN:
                        continue
                    brief = auto_test_brief(c, p, project)
                    if brief == NO_VISIBLE:
                        _NO_VISIBLE_SEEN.add(cid)
                        # Manuel 2026-11-09: nothing a user could see -> nothing for Muse to test -> Done.
                        try:
                            deck_comment('oscar', cid, 'Haiku found nothing visible to an end user on this card, '
                                         'so Muse has nothing to test. Moved to Done without a Muse test.')
                            sess = 'oscar' if int(board) in (27, 30) else 'ringo'
                            move_card_to_done(project, board, stacks_map, c, {'muse_passed': True},
                                              session_agent=sess, all_stacks=all_stacks)
                            log.info('to_test: card %s has no visible effect -> Done (no Muse)', cid)
                        except Exception as e:
                            log.warning('no-visible-effect move failed for card %s: %s', cid, e)
                        continue
                    if brief is None:
                        log.warning('QUEUE_STUCK: card %s on %s is waiting in To test without a TEST-BRIEF marker', cid, project)
                        continue

                target_url = CFG.get('muse_targets', {}).get(project)
                if not target_url:
                    continue

                try:
                    sanitized_brief = muse_feed.sanitize_brief(brief, allow_hosts=tuple(p.get('muse_hosts') or ()))
                    muse_msg = muse_feed.build_muse_message(c['title'], target_url, sanitized_brief)
                except Exception as e:
                    log.warning('failed to build muse message for card %s: %s', cid, e)
                    continue

                c['stackId'] = c.get('stackId') or to_test_stack['id']
                with _lock:
                    STATE.setdefault('deck_active', {})[cid] = f'muse@{project}'
                save_state()
                log.info('to_test: card %s sent to Muse for %s', cid, project)
                POOL.submit(muse_test_job, project, board, stacks_map, c, muse_msg)


def _improvement_route(cid, title):
    room = ((CFG.get('projects') or {}).get('dashboard') or {}).get('room', '')
    return {'type': 'deck', 'project': 'dashboard', 'board': 29, 'card': cid, 'title': title,
            'room': room, 'agent': 'ringo', 'mid': None}


def ask_improvement_approval(entries):
    """Ask Manuel a clear yes/no for the week's proposed improvements; nothing is applied without it."""
    if not entries:
        return None
    lines = ['The weekly review proposes these changes. Nothing to dispatcher behaviour, models, limits '
             'or config is applied until you answer yes.', '']
    for entry in entries:
        lines.append(f"- {entry.get('title')}: before {weekly_review.describe_metric(entry.get('metric'), entry.get('before'))}")
    first = entries[0]
    cid = first.get('cid') or 0
    options = [{'label': 'Yes, approve these', 'action': 'yes'},
               {'label': 'No, keep the current behaviour', 'action': 'no'}]
    return ask_manuel([f"improvements:{first.get('week') or weekly_review.week_key(dt.datetime.now())}"],
                      'Scrum improvements: approve?', '\n'.join(lines),
                      card_url(29, cid) if cid else '', _improvement_route(cid, first.get('title') or ''),
                      options=options)


def create_improvement_cards(to_spec_id, now=None):
    """Create up to 3 deterministic Improvement cards in To spec on the HQ board (no LLM)."""
    now = now or dt.datetime.now()
    payloads = weekly_review.build_improvements(STATE, now)
    if not payloads:
        return []
    if to_spec_id is None:
        to_spec_id = ensure_stack(29, 'To spec')
    if not to_spec_id:
        return []
    label_id = None
    try:
        label_id = ensure_label(29, 'Improvement')
    except Exception:
        log.warning('could not ensure the Improvement label\n%s', traceback.format_exc())
    week = weekly_review.week_key(now)
    created = []
    for payload in payloads[:weekly_review.MAX_IMPROVEMENTS]:
        try:
            made = deck('oscar', 'POST', f'/boards/29/stacks/{to_spec_id}/cards',
                        json={'title': payload['title'], 'description': payload['description'],
                              'order': 0})
            cid = made.get('id') if isinstance(made, dict) else None
            if cid and label_id:
                deck('oscar', 'PUT', f'/boards/29/stacks/{to_spec_id}/cards/{cid}/assignLabel',
                     json={'labelId': label_id})
            entry = {'cid': str(cid) if cid else '', 'week': week, 'title': payload['title'],
                     'metric': payload.get('metric'), 'before': payload.get('before'),
                     'approved': False, 'created_at': time.time()}
            STATE.setdefault('improvements', []).append(entry)
            created.append(entry)
        except Exception:
            log.error('creating an Improvement card failed\n%s', traceback.format_exc())
    save_state()
    if created:
        log.info('weekly review: created %s Improvement cards in To spec on board 29', len(created))
        try:
            ask_improvement_approval(created)
        except Exception:
            log.error('ask_improvement_approval failed\n%s', traceback.format_exc())
    return created


def record_approved_improvements(board_stacks):
    """An Improvement card that left To spec is treated as Manuel-approved: keep its metric baseline."""
    entries = STATE.get('improvements') or []
    if not entries:
        return
    lanes = {}
    for (_project, board), all_stacks in (board_stacks or {}).items():
        if int(board) != 29:
            continue
        for stack in all_stacks or []:
            for card in stack.get('cards') or []:
                lanes[str(card.get('id'))] = str(stack.get('title') or '')
    changed = False
    for entry in entries:
        if entry.get('approved'):
            continue
        lane = lanes.get(str(entry.get('cid')))
        if lane and lane.strip().lower() not in ('to spec', ''):
            entry['approved'] = True
            entry['approved_at'] = time.time()
            changed = True
    if changed:
        save_state()


def check_weekly_review(board_stacks, now=None):
    if not CFG.get('specialists'):
        return
    if now is None:
        now = dt.datetime.now()
    current_week = weekly_review.week_key(now)
    need_improvements = scrum_enabled() and STATE.get('improvement_last_week') != current_week
    if STATE.get('weekly_review_last_week') == current_week and not need_improvements:
        return

    board_29_stacks = board_stacks.get(('dashboard', 29))
    if board_29_stacks is None:
        try:
            board_29_stacks = fetch_stacks(29)
        except Exception:
            return
    if not board_29_stacks:
        return

    existing_cards = []
    to_spec_id = None
    for s in board_29_stacks:
        if s.get('title') == 'To spec':
            to_spec_id = s.get('id')
        for c in s.get('cards') or []:
            c_copy = dict(c)
            c_copy['stackTitle'] = s.get('title')
            existing_cards.append(c_copy)

    if weekly_review.due(now, existing_cards):
        data_block = weekly_review.build_data_block(STATE, now) if scrum_enabled() else ''
        payload = weekly_review.card_payload(now, data_block=data_block)
        if to_spec_id is None:
            to_spec_id = ensure_stack(29, 'To spec')
        session_agent = 'oscar' if 29 in (27, 30) else 'ringo'
        deck(session_agent, 'POST', f"/boards/29/stacks/{to_spec_id}/cards",
             json={'title': payload['title'], 'description': payload['description'], 'order': 0})
        STATE['weekly_review_last_week'] = current_week
        save_state()
        log.info('weekly review: created card on board 29 for %s', current_week)

    # TASK4 part F: the deterministic data block and up to 3 Improvement proposals live with the weekly
    # review. They are only produced while the Scrum flag is on; changes wait for Manuel's yes/no.
    if scrum_enabled() and STATE.get('improvement_last_week') != current_week:
        try:
            create_improvement_cards(to_spec_id, now)
            STATE['improvement_last_week'] = current_week
            save_state()
        except Exception:
            log.error('create_improvement_cards failed\n%s', traceback.format_exc())


# ---------------------------------------------------------------- daily report + Scrum history (TASK4 part B)
LOG_KEEP_SECONDS = 14 * 86400


def _log_ts(entry):
    for key in ('at', 'done_at', 'ts'):
        value = (entry or {}).get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                try:
                    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
                except ValueError:
                    pass
    return 0.0


def _prune_logs(state, now=None):
    """Keep the Scrum timeline keys to 14 days so state.json does not grow forever."""
    now = time.time() if now is None else float(now)
    cutoff = now - LOG_KEEP_SECONDS
    for key in ('done_log', 'return_log', 'escalation_log', 'failure_log', 'quota_log'):
        entries = state.get(key)
        if isinstance(entries, list):
            state[key] = [e for e in entries if _log_ts(e) >= cutoff]
    for key in ('todo_since', 'done_seen'):
        mapping = state.get(key)
        if isinstance(mapping, dict):
            state[key] = {k: v for k, v in mapping.items()
                          if isinstance(v, (int, float)) and v >= cutoff}


def _card_done_ts(card):
    for key in ('done_at', 'completedAt', 'lastModified'):
        value = card.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                try:
                    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
                except ValueError:
                    continue
    return None


def _card_worker_name(card):
    labels = {l.get('title') for l in card.get('labels') or [] if isinstance(l, dict)}
    for name, agent in CFG.get('agents', {}).items():
        if name not in HERMES_AGENTS and agent.get('display') in labels:
            return name
    if card.get('worker') or card.get('agent'):
        return str(card.get('worker') or card.get('agent')).lower()
    return card_assignee_agent(card) or ''


def record_deck_history(board_stacks, now=None):
    """Append Deck Done changes to STATE['done_log'] (14 days) and track when cards enter To do."""
    if not scrum_enabled():
        return
    now = time.time() if now is None else float(now)
    first_run = 'done_seen' not in STATE
    done_seen = STATE.setdefault('done_seen', {})
    done_log = STATE.setdefault('done_log', [])
    todo_since = STATE.setdefault('todo_since', {})
    logged = {str(e.get('cid')) for e in done_log if isinstance(e, dict)}
    for (project, board), all_stacks in (board_stacks or {}).items():
        for stack in all_stacks or []:
            title = stack.get('title')
            cards = stack.get('cards') or []
            if title == 'To do':
                for card in cards:
                    todo_since.setdefault(str(card.get('id')), now)
            elif title in ('Done', 'Finished'):
                for card in cards:
                    cid = str(card.get('id'))
                    if cid in done_seen:
                        continue
                    done_at = _card_done_ts(card) or now
                    done_seen[cid] = done_at
                    # First run: only recent Done cards count; older ones are just marked as seen.
                    if cid in logged or now - done_at > LOG_KEEP_SECONDS:
                        continue
                    if first_run and now - done_at > 86400:
                        continue
                    entry = {'cid': cid, 'title': card.get('title') or '', 'worker': _card_worker_name(card),
                             'project': project, 'board': board, 'done_at': done_at}
                    entered = todo_since.get(cid)
                    if isinstance(entered, (int, float)) and done_at >= entered:
                        entry['cycle_seconds'] = done_at - entered
                    done_log.append(entry)
    _prune_logs(STATE, now)
    save_state()


def deck_snapshot_for(board_stacks):
    """Plain-data snapshot of the Deck stacks fetch_stacks returned this poll."""
    projects = [{'project': project, 'board': board, 'stacks': all_stacks}
                for (project, board), all_stacks in sorted(board_stacks.items(),
                                                           key=lambda kv: (str(kv[0][0]), str(kv[0][1])))]
    return {'projects': projects,
            'workers': [name for name in CFG.get('agents', {}) if name not in HERMES_AGENTS],
            'wip': wip_limits(CFG)}


def _report_datetime(now=None):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(DAILY_REPORT_TZ)
    if now is None:
        return dt.datetime.now(tz)
    if isinstance(now, dt.datetime):
        return now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    return dt.datetime.fromtimestamp(float(now), tz)


def daily_report_due(now=None):
    """True once per Toronto day, from 09:00 onward, until the durable date marker is written."""
    t = _report_datetime(now)
    return t.hour >= DAILY_REPORT_HOUR and STATE.get('daily_report_last_date') != t.date().isoformat()


def maybe_send_daily_report(board_stacks, now=None):
    """Build (no LLM), write outputs/daily-report.md, post HQ Talk once per day, mark the date."""
    if not scrum_enabled() or not daily_report_due(now):
        return None
    text = daily_report.build_report(STATE, deck_snapshot_for(board_stacks), now)
    try:
        os.makedirs(os.path.dirname(DAILY_REPORT_PATH), exist_ok=True)
        tmp = DAILY_REPORT_PATH + '.tmp'
        with open(tmp, 'w') as fh:
            fh.write(text + '\n')
        os.replace(tmp, DAILY_REPORT_PATH)
    except OSError:
        log.error('daily report write failed\n%s', traceback.format_exc())
        return None
    room = (CFG.get('projects', {}).get('hq') or {}).get('room')
    if not room:
        log.error('daily report: no HQ room configured')
        return None
    # Oscar's Hermes cron reads the file and forwards it to Manuel's Telegram; no LLM here.
    mid = talk_post('oscar', room, text, depth=99)
    if mid is None:
        log.error('daily report: HQ post failed; will retry next poll')
        return None
    STATE['daily_report_last_date'] = _report_datetime(now).date().isoformat()
    save_state()
    log.info('daily report written to %s and posted to HQ', DAILY_REPORT_PATH)
    return text


def poll_deck():
    freddy_labelled = set(); failed_boards = set(); ok_boards = set(); decide_now = {}
    freddy_sent = False
    started_this_poll = set()
    board_stacks = {}
    frozen = frozen_allow()
    for project, p in CFG['projects'].items():
        for board in p['boards']:
            try:
                all_stacks = fetch_stacks(board)
            except Exception as e:
                all_stacks = None; log.warning('deck board %s: %s', board, e)
            if all_stacks is None:
                failed_boards.add(project); continue
            ok_boards.add(board)
            board_stacks[(project, board)] = all_stacks
            stacks = {s['title']: s['id'] for s in all_stacks}
            for s in all_stacks:
                if s['title'] not in ('Done', 'Finished'):
                    for c in s.get('cards') or []:
                        if any(l['title'] == 'Decide' for l in c.get('labels') or []):
                            decide_now[str(c['id'])] = (project, board, c)
                if s['title'] != 'To do':
                    continue
                for c in s.get('cards') or []:
                    labels = {l['title'] for l in c.get('labels') or []}
                    if labels & {'Manuel', 'Decide'} or str(c['id']) in STATE['deck_active']:
                        continue
                    if 'Freddy' in labels:
                        freddy_labelled.add(project)
                    if frozen is not None and str(c['id']) not in frozen:
                        continue
                    agent = next((n for n, a in CFG['agents'].items() if n not in HERMES_AGENTS and a['display'] in labels), None)
                    if not agent:
                        # Manuel 2026-10-20: assigning an agent's Nextcloud user to the card works like its label
                        agent = card_assignee_agent(c)
                    if not agent:
                        continue
                    if agent in ('ringo', 'miku') and (claude_limited() or claude_down()):
                        if not claude_down():
                            continue   # session-limit gate: leave it in To do until the reset
                        # No-Claude mode (manual switch): do not stop the pipeline. Freddy takes the card;
                        # its review becomes a NO_CLAUDE note; integration waits for Ringo (Manuel 2026-10-27).
                        ok, _reason = freddy_ready()
                        if not ok or not relabel_card_to_freddy(board, c, display(agent)):
                            continue
                        log.info('no-claude: card %s %s -> Freddy', c['id'], display(agent))
                        agent = 'freddy'
                    if agent_limited(agent):   # quota/auth/limit back-off: leave it in To do (TASK3)
                        continue
                    # one card in flight per project (LM-00): the next card waits until the previous one
                    # is fully done INCLUDING Ringo's review, so two cards never share an uncommitted tree.
                    # Ringo cards (report triage, DR-300) only read and write cards, so they run alongside;
                    # one Ringo card per project at a time.
                    busy = [v for v in in_flight() if v.endswith('@' + project)]
                    if (busy if agent != 'ringo' else [v for v in busy if v.startswith('ringo@')]):
                        continue
                    # Manuel 2026-10-20: a fallback project (LucidMail behind DocReview) gives Freddy work only
                    # while the primary has nothing he can start: no card of it in flight and no Freddy card
                    # ready, or its work-hours gate is closed. Projects are polled in company.json order,
                    # so the primary's To do was already seen in this poll. 2026-10-25: and only once the
                    # primary has been idle for fallback_grace_minutes, so Ringo's idle nudge can label its
                    # next card first (else the fallback card yields after one round).
                    prim = p.get('fallback_for')
                    if prim and agent == 'freddy':
                        pp = CFG['projects'][prim]
                        gated = pp.get('outside_work_hours') and in_work_hours()
                        if any(v.endswith('@' + prim) for v in in_flight()) or \
                                (prim in freddy_labelled and not gated):
                            continue
                        if not gated and (prim in failed_boards or time.time() - PRIMARY_IDLE.get(prim, time.time())
                                          < p.get('fallback_grace_minutes', 0) * 60):
                            continue
                    # outside_work_hours: a project whose cards may only start outside Manuel's work hours
                    # (DocReview had it until 2026-10-20; agent commits are now allowed any hour)
                    if p.get('outside_work_hours') and in_work_hours():
                        continue
                    # 2026-10-30: at most one new Freddy card per poll, so company.json order is his priority
                    # (ClientA before DocReview); two dispatched in one poll raced for FREDDY_LOCK.
                    if agent == 'freddy' and freddy_sent:
                        continue
                    if agent == 'freddy' and not p.get('miku_fallback', True):
                        ok, _reason = freddy_ready()
                        if not ok and deepti_for(project) and not DEEPTI_LOCK.locked():
                            ok = True   # execute() hands it to Deepti
                        if not ok:
                            continue  # leave it in To do; retried automatically next poll
                    with _lock:
                        STATE['deck_active'][str(c['id'])] = f'{agent}@{project}'
                    save_state()
                    log.info('deck -> %s project=%s card=%s', agent, project, c['id'])
                    POOL.submit(deck_job, agent, project, board, stacks, c)
                    started_this_poll.add(agent)
                    freddy_sent = freddy_sent or agent == 'freddy'
    if scrum_enabled():
        try:
            grandfather_current_todo(board_stacks)
        except Exception:
            log.error('grandfather_current_todo failed\n%s', traceback.format_exc())
    if CFG.get('pull_queue', False):
        agent_labels = all_agent_labels()
        # Dead-worker detection each tick, before pulling: a returned card can be pulled again this same poll.
        try:
            check_stalled_claims(board_stacks)
        except Exception:
            log.error('check_stalled_claims failed\n%s', traceback.format_exc())
        pull_workers = [a for a in PULL_WORKERS if a in CFG.get('agents', {})]
        for agent in pull_workers:
            if agent in started_this_poll:
                continue
            lock = CODER_LOCKS.get(agent)
            if lock and lock.locked():
                continue
            if agent_limited(agent):   # quota/auth/limit back-off: skip until the limit resets (TASK3)
                continue
            if agent == 'freddy':
                ok, _reason = freddy_ready()
                if not ok:
                    continue
            elif agent == 'deepti':
                if deepti_seconds_left() <= 0:
                    continue
            if scrum_enabled() and worker_wip_hit(agent, board_stacks):
                log.info('WIP: %s is at the Doing limit; not pulling another card', agent)
                continue

            pulled = False
            for project, p in CFG['projects'].items():
                if not p.get('repo'):
                    continue
                if agent in ('caine', 'gem', 'deepti'):
                    if project not in CFG['agents'][agent].get('projects', []):
                        continue
                if p.get('outside_work_hours') and in_work_hours():
                    continue
                if agent == 'freddy':
                    prim = p.get('fallback_for')
                    if prim:
                        pp = CFG['projects'][prim]
                        gated = pp.get('outside_work_hours') and in_work_hours()
                        if any(v.endswith('@' + prim) for v in in_flight()) or \
                                (prim in freddy_labelled and not gated):
                            continue
                        if not gated and (prim in failed_boards or time.time() - PRIMARY_IDLE.get(prim, time.time())
                                          < p.get('fallback_grace_minutes', 0) * 60):
                            continue
                    if not p.get('miku_fallback', True):
                        ok, _reason = freddy_ready()
                        if not ok:
                            continue

                # TASK4 part E: stop starting, start finishing. At the To review / To test limit, a
                # worker takes a finishing task instead of another To do card from this project.
                if scrum_enabled():
                    at_limit, reasons = wip_hit(board_stacks, project)
                    if at_limit:
                        log.info('WIP: %s at %s; not pulling a new card there',
                                 project, ', '.join(reasons))
                        continue

                # One card in flight per project
                if any(v.endswith('@' + project) for v in in_flight()):
                    continue

                for board in p.get('boards', []):
                    all_stacks = board_stacks.get((project, board))
                    if all_stacks is None:
                        continue
                    stacks = {s['title']: s['id'] for s in all_stacks}
                    todo_stack = next((s for s in all_stacks if s['title'] == 'To do'), None)
                    if not todo_stack:
                        continue
                    for c in todo_stack.get('cards') or []:
                        if str(c['id']) in STATE['deck_active']:
                            continue
                        if frozen is not None and str(c['id']) not in frozen:
                            continue
                        card_labels = {l['title'] for l in c.get('labels') or []}
                        if card_labels & (EXCLUDED_PULL_LABELS | agent_labels):
                            continue
                        if card_assignee_agent(c):
                            continue
                        if scrum_enabled() and not is_grandfathered(c):
                            check = dict(c)
                            check['project'] = project
                            ready, missing = is_ready(check)
                            if not ready:
                                return_card_to_to_spec(project, board, all_stacks, c, missing, agent)
                                continue

                        c['stackId'] = c.get('stackId') or todo_stack['id']
                        agent_label = display(agent)
                        if not label_card_for_pull(board, c, agent):
                            continue
                        log.info('pull: card %s -> %s project=%s', c['id'], agent_label, project)
                        claim_card(c['id'], agent, project, board)
                        with _lock:
                            STATE['deck_active'][str(c['id'])] = f'{agent}@{project}'
                        save_state()
                        log.info('deck -> %s project=%s card=%s', agent, project, c['id'])
                        POOL.submit(deck_job, agent, project, board, stacks, c)
                        started_this_poll.add(agent)
                        if agent == 'freddy':
                            freddy_sent = True
                            freddy_labelled.add(project)
                        pulled = True
                        break
                    if pulled:
                        break
                if pulled:
                    break
            # TASK4 part G: empty queue -> review needed > test needed > unblock > improvement cards.
            if scrum_enabled() and not pulled:
                # deck_job can review/unblock/improve but cannot test; To-test cards belong to Muse
                # (re-running a To-test card looped card 416 ten times on 2026-11-09). 'test' stays with Muse.
                candidate = pick_self_organizing_task(agent, board_stacks, STATE, frozen,
                                                      kinds=('review', 'unblock', 'improvement'))
                if candidate:
                    project = candidate['project']
                    board = candidate['board']
                    all_stacks = candidate['stacks']
                    c = candidate['card']
                    c['stackId'] = c.get('stackId') or next(
                        (s['id'] for s in all_stacks if s.get('title') == 'To do'), None)
                    stacks_map = {s['title']: s['id'] for s in all_stacks}
                    if label_card_for_pull(board, c, agent):
                        claim_card(c['id'], agent, project, board)
                        with _lock:
                            STATE['deck_active'][str(c['id'])] = f'{agent}@{project}'
                        save_state()
                        log.info('self-organizing: %s takes %s card %s on %s',
                                 agent, candidate['kind'], c['id'], project)
                        POOL.submit(deck_job, agent, project, board, stacks_map, c)
                        started_this_poll.add(agent)
                        if agent == 'freddy':
                            freddy_sent = True
                            freddy_labelled.add(project)
    for prim in {p['fallback_for'] for p in CFG['projects'].values() if p.get('fallback_for')}:
        if prim in freddy_labelled or prim in failed_boards or any(v.endswith('@' + prim) for v in in_flight()):
            PRIMARY_IDLE.pop(prim, None)
        else:
            PRIMARY_IDLE.setdefault(prim, time.time())
    FREDDY_WANTED.clear()
    FREDDY_WANTED.update(freddy_labelled - failed_boards)
    decide_check(decide_now, ok_boards)
    freddy_nudge_check(freddy_labelled, failed_boards)
    if CFG.get('specialists'):
        try:
            check_weekly_review(board_stacks)
        except Exception:
            log.error('check_weekly_review failed\n%s', traceback.format_exc())
        try:
            poll_to_spec(board_stacks)
        except Exception:
            log.error('poll_to_spec failed\n%s', traceback.format_exc())
        try:
            poll_to_test(board_stacks)
        except Exception:
            log.error('poll_to_test failed\n%s', traceback.format_exc())
    if scrum_enabled():
        try:
            record_deck_history(board_stacks)
        except Exception:
            log.error('record_deck_history failed\n%s', traceback.format_exc())
        try:
            record_approved_improvements(board_stacks)
        except Exception:
            log.error('record_approved_improvements failed\n%s', traceback.format_exc())
        if ok_boards:
            try:
                maybe_send_daily_report(board_stacks)
            except Exception:
                log.error('maybe_send_daily_report failed\n%s', traceback.format_exc())


def decide_check(decide_now, ok_boards):
    """A card that gets the Decide label (by Ringo or a human; never a worker failure, TASK3) goes to
    Manuel's Telegram once. The first scan after this feature went live only records the cards already waiting."""
    seeding = 'decide_seen' not in STATE
    seen = STATE.setdefault('decide_seen', {})
    for cid in [k for k, (b, t) in seen.items() if b in ok_boards and k not in decide_now and time.time() - t > 300]:
        with _lock:   # label gone (5-min margin: deck_job marks a card just before it labels it)
            seen.pop(cid, None)
    for cid, (project, board, c) in decide_now.items():
        if seeding:
            seen[cid] = [board, 0]; continue
        key = mark_decide(cid, board)
        if not key:
            continue
        try:
            r = SESS['oscar'].get(f"{DECK_OCS}/cards/{cid}/comments", params={'limit': 3}, timeout=30)
            last = r.json()['ocs']['data'][0] if r.status_code == 200 and r.json()['ocs']['data'] else None
        except (requests.RequestException, ValueError, KeyError):
            last = None
        body = (f"{last['actorDisplayName']}: {last['message']}" if last else (c.get('description') or '(no description)'))
        ask_manuel([key], f"{PNAME.get(project, project)} #{cid} «{c['title']}» · decisión pendiente", body,
                   card_url(board, cid), deck_route(project, board, c))
    save_state()


IDLE_NUDGE_SECS = 2 * 3600


def freddy_nudge_check(labelled, failed):
    """Freddy is idle and nothing on a project is labelled Freddy: ask Ringo to label the next ready cards.
    Freddy busy on a FALLBACK project still counts as idle for its primary (2026-10-21: the DocReview
    queue ran dry at 02:27 and nobody topped it up while he worked LucidMail)."""
    if CFG.get('pull_queue', False):
        return
    if os.path.exists(PAUSE_FLAG) or claude_limited() or frozen_allow() is not None:
        return
    no_claude = claude_down()
    freddy_on = {v.split('@', 1)[1] for v in STATE['deck_active'].values() if v.startswith('freddy@')}
    fallbacks = {n for n, p in CFG['projects'].items() if p.get('fallback_for')}
    primaries = {p['fallback_for'] for p in CFG['projects'].values() if p.get('fallback_for')}
    only_primaries = bool(freddy_on) or FREDDY_LOCK.locked()
    if only_primaries and not (freddy_on and freddy_on <= fallbacks):
        return
    nudges = STATE.setdefault('idle_nudge', {})
    now = time.time()
    for project, p in CFG['projects'].items():
        if project in labelled or project in failed or not p.get('repo') or not p['boards']:
            continue
        if only_primaries and project not in primaries:
            continue
        if p.get('outside_work_hours') and in_work_hours():
            continue
        if now - nudges.get(project, 0) < IDLE_NUDGE_SECS:
            continue
        ok, _reason = brain_available()
        if not ok:
            return
        nudges[project] = now
        save_state()
        # Manuel 2026-10-30: nudges go to Oscar (Haiku) always, not Opus Ringo; the else branch is kept for rollback
        if True:
            talk_post('oscar', p['room'], "@oscar " + ("No-Claude mode is ON (Ringo offline). " if no_claude else "") + "Freddy is idle and no card "
                      "on this board is labelled Freddy. Review the To do cards yourself and label the next one or "
                      "two that are ready for development (spec complete, acceptance criteria and dependencies met) "
                      "with the Freddy label, in dependency order. Do not label cards that need a decision from "
                      "Manuel or are not ready. Reply with the cards you labelled, or 'none ready' with the blocker.",
                      depth=0)
        else:
            talk_post('oscar', p['room'], "@ringo Freddy is idle and no card on this board is labelled Freddy. "
                      "Review the To do cards and label the next one or two that are ready for development "
                      "(spec complete, acceptance criteria and dependencies met) with the Freddy label, in dependency order. "
                      "Do not label cards that need a decision from Manuel or are not ready. "
                      "Reply with the cards you labelled and why, or 'none ready' with the blocker.", depth=0)
        log.info('idle nudge -> oscar project=%s', project)
        return


# ---------------------------------------------------------------- restart recovery
# A restart kills every running job (needrestart did it 2026-10-25 11:20: #350 stayed in Doing and the next card
# started in its uncommitted tree; #311 on 2026-10-22). Cards that were in deck_active are recovered on start,
# before poll_deck may start another card of that project (in_flight() counts them as busy).
RECOVER_TRIES = 20


def snapshot_wip(repo, cid, agent):
    """Commit a killed run's WIP on the card's own branch (freddy/DR-<id>) with the agent's identity, so the next
    card cannot carry it along. Other branches (LucidMail works on main) keep it uncommitted. Returns a note."""
    env = dict(os.environ, **agent_git_env(agent))
    g = lambda *a: subprocess.run(['git', '-C', repo, *a], capture_output=True, text=True, timeout=120, env=env)
    branch = g('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    if not g('status', '--porcelain', '--', '.', ':!.qwen').stdout.strip():
        return f'The tree in {repo} ({branch}) had no uncommitted changes.'
    if not re.search(rf'(?<!\d){cid}(?!\d)', branch):
        return (f'Its uncommitted edits stay in {repo} on {branch} (not a card branch, so not committed): '
                'the next run continues from `git status` / `git diff`.')
    g('add', '-A', '--', '.', ':!.qwen')
    r = g('commit', '--no-verify', '-q', '-m',
          f'WIP card #{cid}: snapshot of an interrupted {display(agent)} run (dispatcher restart), unreviewed')
    if r.returncode:
        return f'Committing its WIP on {branch} failed ({(r.stderr or r.stdout).strip()[-200:]}); the edits stay uncommitted in {repo}.'
    sha = g('rev-parse', '--short', 'HEAD').stdout.strip()
    return (f'Its uncommitted edits were committed as {sha} "WIP card #{cid} ..." on {branch} (unreviewed): '
            f'continue from that commit (`git show {sha}`), do not start over.')


def recover_card(cid, who):
    agent, project = who.split('@', 1)
    release_claim(cid)   # a restart recovered this card; no claim to watch anymore
    p = CFG['projects'][project]
    for board in p['boards']:
        stacks = deck('oscar', 'GET', f'/boards/{board}/stacks')
        card = next((c for s in stacks for c in s.get('cards') or [] if str(c['id']) == cid), None)
        if card:
            break
    else:
        log.info('recover: card %s not on the %s boards (archived/deleted); left alone', cid, project); return
    ids = {s['title']: s['id'] for s in stacks}
    where = next((s['title'] for s in stacks if s['id'] == card['stackId']), '?')
    labels = {l['title'] for l in card.get('labels') or []}
    if where != 'Doing' or labels & {'Manuel', 'Decide'} or 'To do' not in ids:
        log.info('recover: card %s is in %s with %s; a human moved it, left alone', cid, where, sorted(labels)); return
    note = snapshot_wip(p['repo'], cid, agent) if agent != 'ringo' and p.get('repo') else ''
    deck_comment('oscar', cid, f"A dispatcher restart stopped {display(agent)}'s run on this card. "
                 + (note + ' ' if note else '') + 'Requeued at the top of To do.')
    path = f"/boards/{board}/stacks/{card['stackId']}/cards/{cid}"
    lab = display(agent)
    if lab not in labels and not card_assignee_agent(card):
        blabels = {l['title']: l['id'] for l in deck('oscar', 'GET', f'/boards/{board}')['labels']}
        if lab in blabels:
            deck('oscar', 'PUT', path + '/assignLabel', json={'labelId': blabels[lab]})
    deck('oscar', 'PUT', path + '/reorder', json={'order': 0, 'stackId': ids['To do']})
    log.info('recover: card %s (%s) requeued at the top of To do. %s', cid, who, note[:160])


def recover_orphans():
    rec = STATE.get('recover') or {}
    for cid, r in list(rec.items()):
        try:
            recover_card(cid, r['who']); done = True
        except Exception:
            r['tries'] = r.get('tries', 0) + 1
            done = r['tries'] >= RECOVER_TRIES
            log.error('recover card %s failed (try %s)\n%s', cid, r['tries'], traceback.format_exc())
        if done:
            with _lock:
                rec.pop(cid, None)
    save_state()


def main():
    with _lock:   # jobs do not survive a restart: their cards are recovered first
        rec = STATE.setdefault('recover', {})
        held = STATE.get('pending_reviews') or {}
        for cid, who in (STATE.get('deck_active') or {}).items():
            if cid not in held:   # held reviews keep their card in Doing; resume_held reruns them
                rec.setdefault(cid, {'who': who})
        STATE['deck_active'] = {cid: f"{pr['agent']}@{pr['project']}" for cid, pr in held.items()}
    save_state()
    banner = f' ({dispatcher_name()})' if scrum_enabled() else ''
    log.info('dispatcher start%s%s', banner, f' (recovering cards {sorted(rec)})' if rec else '')
    if CFG.get('specialists'):
        for p in CFG.get('projects', {}).values():
            for b in p.get('boards', []):
                try:
                    ensure_stack(b, 'To test')
                except Exception:
                    log.error('ensure_stack failed for board %s\n%s', b, traceback.format_exc())
                try:
                    ensure_label(b, 'Argon')
                except Exception:
                    log.error('ensure_label failed for board %s\n%s', b, traceback.format_exc())
    while True:
        if STATE.get('recover'):
            recover_orphans()
        if os.path.exists(STOP_FLAG):
            try:
                mode = open(STOP_FLAG).read().strip().lower()
            finally:
                os.remove(STOP_FLAG)
            stop_freddy('Manuel (via Oscar)', handoff=(mode != 'cancel'))
        for fn in (poll_talk, check_tasks, resume_held, poll_deck, poll_deck_mentions):
            try:
                fn()
            except Exception:
                log.error('%s failed\n%s', fn.__name__, traceback.format_exc())
        time.sleep(CFG['poll_seconds'])


if __name__ == '__main__':
    main()
