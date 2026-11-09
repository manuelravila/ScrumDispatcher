"""2026-10-25 (Manuel voice 2104): TOOK_OVER limit, fallback grace + min rounds, restart recovery, board 403
backoff, Decide/Talk questions for Manuel on Telegram (tools/manuel_ask.py)."""
import json, os, sqlite3, subprocess, tempfile, time, unittest
from unittest import mock
import requests
from isolate import d, real_tree_snapshot, TMP

ma = d.manuel_ask


def card(cid, labels=(), stack=1, title='T'):
    return {'id': cid, 'title': title, 'stackId': stack, 'labels': [{'title': l} for l in labels],
            'assignedUsers': [], 'description': 'desc'}


def git(repo, *a):
    return subprocess.run(['git', '-C', repo, *a], capture_output=True, text=True, check=True).stdout.strip()


def temp_repo(branch):
    repo = tempfile.mkdtemp(dir=TMP)
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'init')
    if branch != 'main':
        git(repo, 'checkout', '-q', '-b', branch)
    open(os.path.join(repo, 'a.py'), 'w').write('x = 1\n')
    os.makedirs(os.path.join(repo, '.qwen')); open(os.path.join(repo, '.qwen', 's.json'), 'w').write('{}')
    return repo


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = json.loads(json.dumps(d.STATE))
        d.STATE.update({'deck_active': {}, 'last_id': {}, 'depth': {}})
        for k in ('recover', 'decide_seen', 'decide_gen', 'fix_rounds'):
            d.STATE.pop(k, None)
        d.PRIMARY_IDLE.clear(); d.BOARD_BACKOFF.clear(); d.FREDDY_WANTED.clear()
        if os.path.exists(ma.DB):
            os.remove(ma.DB)

    def tearDown(self):
        d.STATE.clear(); d.STATE.update(self.saved)


class FallbackTest(Base):
    def setUp(self):
        super().setUp()
        # the fixture: LucidMail a fallback behind DocReview and polled after it, as before 2026-10-31
        # (live company.json made LucidMail primary and moved it ahead of DocReview that day)
        projects = {k: dict(v) for k, v in d.CFG['projects'].items()}
        projects['lucidmail']['fallback_for'] = 'docreview'
        projects['lucidmail'] = projects.pop('lucidmail')
        patcher = mock.patch.dict(d.CFG, projects=projects)
        patcher.start(); self.addCleanup(patcher.stop)

    def boards(self, dr_ready=False):
        todo = lambda cards: [{'title': 'To do', 'id': 1, 'cards': cards}, {'title': 'Doing', 'id': 2, 'cards': []}]
        def fetch(board):
            if board == 20:
                return todo([card(219, ['Freddy'])])
            if board == 15 and dr_ready:
                return todo([card(400, ['Freddy'])])
            return todo([])
        return fetch

    def poll(self, dr_ready=False):
        started = []
        with mock.patch.object(d, 'fetch_stacks', side_effect=self.boards(dr_ready)), \
             mock.patch.object(d, 'freddy_ready', return_value=(True, '')), \
             mock.patch.object(d, 'in_work_hours', return_value=False), \
             mock.patch.object(d, 'freddy_nudge_check'), mock.patch.object(d, 'decide_check'), \
             mock.patch.object(d.POOL, 'submit', side_effect=lambda fn, agent, project, board, stacks, c: started.append(c['id'])):
            d.poll_deck()
        d.STATE['deck_active'].clear()
        return started

    def test_fallback_waits_for_the_grace_period(self):
        self.assertEqual(self.poll(), [])                       # docreview just went idle: LucidMail waits
        self.assertIn('docreview', d.PRIMARY_IDLE)
        self.assertEqual(self.poll(), [])
        d.PRIMARY_IDLE['docreview'] = time.time() - 21 * 60     # 20-min grace (company.json) passed
        self.assertEqual(self.poll(), [219])

    def test_ready_primary_card_resets_the_grace(self):
        d.PRIMARY_IDLE['docreview'] = time.time() - 3600
        self.assertEqual(self.poll(dr_ready=True), [400])
        self.assertNotIn('docreview', d.PRIMARY_IDLE)

    def test_orphan_being_recovered_counts_as_busy(self):
        d.PRIMARY_IDLE['docreview'] = time.time() - 3600
        d.STATE['recover'] = {'219': {'who': 'freddy@lucidmail'}}
        self.assertEqual(self.poll(), [])

    def test_min_rounds_before_yield(self):
        d.FREDDY_WANTED.add('docreview')
        runs = []
        def run(project, prompt):
            runs.append(1); raise d.FreddyTimeout('half')
        with mock.patch.dict(d.CFG['projects']['lucidmail'], fallback_min_rounds=2), \
             mock.patch.object(d, 'run_freddy', side_effect=run), \
             mock.patch.object(d, 'tree_state', side_effect=lambda r: object()), \
             mock.patch.object(d, 'build_prompt', return_value='p'), \
             mock.patch.object(d.os.path, 'exists', return_value=False):
            with self.assertRaises(d.FreddyYield) as cm:
                d.run_freddy_rounds('lucidmail', 't', 'c', lambda n: None)
        self.assertEqual(cm.exception.args[1], 2)
        self.assertEqual(len(runs), 2)


class RecoverTest(Base):
    def test_wip_on_card_branch_is_committed_as_the_agent(self):
        repo = temp_repo('freddy/DR-350')
        note = d.snapshot_wip(repo, '350', 'freddy')
        self.assertIn('committed as', note)
        self.assertEqual(git(repo, 'status', '--porcelain'), '?? .qwen/')
        self.assertIn('freddy@agents.automatos.ca', git(repo, 'log', '-1', '--format=%ae %ce'))
        self.assertIn('WIP card #350', git(repo, 'log', '-1', '--format=%s'))

    def test_wip_on_main_stays_uncommitted(self):
        repo = temp_repo('main')
        self.assertIn('not a card branch', d.snapshot_wip(repo, '219', 'freddy'))
        self.assertIn('a.py', git(repo, 'status', '--porcelain'))
        self.assertIn('freddy/DR-3500', 'freddy/DR-3500')   # id boundary: 350 must not match DR-3500
        repo2 = temp_repo('freddy/DR-3500')
        self.assertIn('not a card branch', d.snapshot_wip(repo2, '350', 'freddy'))

    def run_recover(self, stack_title, labels=('Freddy', 'To review')):
        stacks = [{'title': 'To do', 'id': 1, 'cards': []}, {'title': 'Doing', 'id': 2, 'cards': []},
                  {'title': 'Done', 'id': 3, 'cards': []}]
        sid = next(s['id'] for s in stacks if s['title'] == stack_title)
        next(s for s in stacks if s['id'] == sid)['cards'].append(card(350, labels, stack=sid))
        calls, comments = [], []
        def fake_deck(agent, method, path, **kw):
            calls.append((method, path, kw.get('json')))
            if path.endswith('/stacks'):
                return stacks
            return {'labels': [{'title': 'Freddy', 'id': 7}]}
        with mock.patch.object(d, 'deck', side_effect=fake_deck), \
             mock.patch.object(d, 'deck_comment', side_effect=lambda a, c, t: comments.append(t)), \
             mock.patch.object(d, 'snapshot_wip', return_value='WIP note.'), \
             mock.patch.dict(d.CFG['projects']['docreview'], boards=[15]):
            d.STATE['recover'] = {'350': {'who': 'freddy@docreview'}}
            d.recover_orphans()
        self.assertEqual(d.STATE['recover'], {})
        return calls, comments

    def test_doing_card_goes_back_on_top_of_todo(self):
        calls, comments = self.run_recover('Doing')
        self.assertIn(('PUT', '/boards/15/stacks/2/cards/350/reorder', {'order': 0, 'stackId': 1}), calls)
        self.assertIn('WIP note.', comments[0])

    def test_label_restored_when_missing(self):
        calls, _ = self.run_recover('Doing', labels=())
        self.assertIn(('PUT', '/boards/15/stacks/2/cards/350/assignLabel', {'labelId': 7}), calls)

    def test_card_a_human_moved_is_left_alone(self):
        calls, comments = self.run_recover('Done')
        self.assertFalse([c for c in calls if c[0] == 'PUT'])
        self.assertEqual(comments, [])

    def test_startup_moves_deck_active_to_recover(self):
        d.STATE['deck_active'] = {'219': 'freddy@lucidmail'}
        seen = {}
        def stop(_):
            raise KeyboardInterrupt
        with mock.patch.object(d, 'recover_orphans', side_effect=lambda: seen.update(d.STATE['recover'])), \
             mock.patch.object(d, 'poll_talk'), mock.patch.object(d, 'check_tasks'), mock.patch.object(d, 'poll_deck'), \
             mock.patch.object(d.time, 'sleep', side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):
                d.main()
        self.assertEqual(seen, {'219': {'who': 'freddy@lucidmail'}})
        self.assertEqual(d.STATE['deck_active'], {})


class BoardBackoffTest(Base):
    def test_403_warns_once_and_backs_off(self):
        resp = mock.Mock(status_code=403)
        err = requests.HTTPError('403 Forbidden', response=resp)
        with mock.patch.object(d, 'deck', side_effect=err) as dk, mock.patch.object(d, 'log') as lg:
            self.assertIsNone(d.fetch_stacks(28))
            self.assertIsNone(d.fetch_stacks(28))
        self.assertEqual(dk.call_count, 1)
        self.assertEqual(lg.warning.call_count, 1)
        d.BOARD_BACKOFF[28] = 0
        with mock.patch.object(d, 'deck', return_value=[]), mock.patch.object(d, 'log') as lg:
            self.assertEqual(d.fetch_stacks(28), [])
        self.assertNotIn(28, d.BOARD_BACKOFF)


class TookOverTest(Base):
    def prompts(self, replies, key='card-9'):
        sent = []
        def fake(agent, project, task, context, notify, thread_key=None):
            if agent == 'ringo':
                sent.append(task); return 'ringo', replies.pop(0), None
            return 'freddy', 'fixed', None
        with mock.patch.object(d, 'execute', side_effect=fake):
            out = d.review_loop('docreview', 't', 'freddy', 'r', key, lambda n: None)
        return out, sent

    def test_trivial_only_until_freddy_failed_twice(self):
        (verdict, *_), sent = self.prompts(['FIXES\n1. a', 'FIXES\n1. b', 'TOOK_OVER\ndone'])
        self.assertEqual(verdict, 'TOOK_OVER')
        self.assertIn('ONLY for a trivial fix', sent[0])
        self.assertIn('1 fix round(s)', sent[1])
        self.assertIn('already failed twice', sent[2])
        self.assertNotIn('card-9', d.STATE['fix_rounds'])     # reset after the card is done

    def test_fix_rounds_survive_a_requeue(self):
        d.STATE['fix_rounds'] = {'card-9': 2}
        _, sent = self.prompts(['APPROVED'])
        self.assertIn('already failed twice', sent[0])

    def test_changed_lines_counts_tree_and_commits(self):
        repo = temp_repo('freddy/DR-1')
        git(repo, 'add', 'a.py'); git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'a')
        base = real_tree_snapshot(repo)
        open(os.path.join(repo, 'a.py'), 'a').write('y = 2\nz = 3\n')
        git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'b')
        open(os.path.join(repo, 'a.py'), 'a').write('w = 4\n')
        self.assertEqual(d.changed_lines(repo, base), 3)


    def test_changed_lines_ignores_coder_untracked_files(self):
        # card 361: Freddy's new files, untracked at review start and committed by Ringo, were counted as Ringo's
        repo = temp_repo('freddy/DR-1')
        git(repo, 'add', 'a.py'); git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'a')
        open(os.path.join(repo, 'new.py'), 'w').write('a = 1\nb = 2\nc = 3\n')
        base = real_tree_snapshot(repo)
        git(repo, 'add', '-A'); git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'wip')
        open(os.path.join(repo, 'new.py'), 'a').write('d = 4\n')
        self.assertEqual(d.changed_lines(repo, base), 1)
        self.assertEqual(git(repo, 'status', '--porcelain').strip(), 'M new.py')   # real index untouched

class TalkTest(Base):
    def poll(self, msgs):
        room = d.CFG['projects']['docreview']['room']
        d.STATE['last_id'] = {p['room']: 1 for p in d.CFG['projects'].values()}
        def get(url, params=None, timeout=None):
            r = mock.Mock(status_code=200 if url.endswith(room) else 304)
            r.json.return_value = {'ocs': {'data': msgs}}
            return r
        jobs, asks = [], []
        with mock.patch.object(d.SESS['oscar'], 'get', side_effect=get), \
             mock.patch.object(d, 'ask_manuel', side_effect=lambda *a, **k: asks.append(a)), \
             mock.patch.object(d, 'task_job_start'), \
             mock.patch.object(d.POOL, 'submit', side_effect=lambda fn, agent, *a: jobs.append(agent)):
            d.poll_talk()
        return jobs, asks

    def msg(self, mid, actor, text, params=None):
        return {'id': mid, 'actorId': actor, 'actorType': 'users', 'actorDisplayName': actor, 'messageType': 'comment',
                'message': text, 'messageParameters': params or {}}

    def test_agent_question_to_manuel_is_asked_once_per_message(self):
        jobs, asks = self.poll([self.msg(5, 'claude', '{mention-user1} merge DR-66 now?\nOPTION: yes\nOPTION: later',
                                         {'mention-user1': {'type': 'user', 'id': 'mrar1995'}})])
        self.assertEqual(jobs, [])
        self.assertEqual(asks[0][0], ['talk:5'])
        self.assertEqual(asks[0][4]['agent'], 'ringo')

    def test_relay_from_telegram_routes_like_manuel(self):
        jobs, asks = self.poll([self.msg(6, 'oscar', ma.RELAY_PREFIX + ' @oscar sí, adelante')])
        self.assertEqual(jobs, ['oscar'])          # an agent's @oscar without '?' would be dropped
        self.assertEqual(asks, [])

    def test_plain_agent_message_is_not_asked(self):
        _, asks = self.poll([self.msg(7, 'claude', 'done, Pending: nothing')])
        self.assertEqual(asks, [])


class DecideTest(Base):
    def test_seed_then_one_ask_per_decide_spell(self):
        asks = []
        c = card(60, ['Decide'])
        with mock.patch.object(d, 'ask_manuel', side_effect=lambda *a, **k: asks.append(a[0])), \
             mock.patch.object(d.SESS['oscar'], 'get', side_effect=requests.ConnectionError):
            d.decide_check({'60': ('docreview', 15, c)}, {15})     # first scan: only recorded
            self.assertEqual(asks, [])
            d.decide_check({'60': ('docreview', 15, c), '61': ('docreview', 15, card(61, ['Decide']))}, {15})
            d.decide_check({'60': ('docreview', 15, c), '61': ('docreview', 15, card(61, ['Decide']))}, {15})
            self.assertEqual(asks, [['decide:61:1']])
            d.decide_check({'60': ('docreview', 15, c)}, {15})      # 61 lost its label a moment ago: kept
            d.STATE['decide_seen']['61'][1] -= 301
            d.decide_check({'60': ('docreview', 15, c)}, {15})      # ...and 5 min later forgotten
            d.decide_check({'60': ('docreview', 15, c), '61': ('docreview', 15, card(61, ['Decide']))}, {15})
        self.assertEqual(asks, [['decide:61:1'], ['decide:61:2']])

    def test_deck_job_key_blocks_the_scan(self):
        d.STATE['decide_seen'] = {}
        self.assertEqual(d.mark_decide(70, 15), 'decide:70:1')
        with mock.patch.object(d, 'ask_manuel') as am:
            d.decide_check({'70': ('docreview', 15, card(70, ['Decide']))}, {15})
        am.assert_not_called()


class ManuelAskTest(Base):
    def test_dedupe_and_options(self):
        body, opts = ma.parse_options('Merge DR-66?\nOPTION: merge now\n- OPTION 2: wait for #55')
        self.assertEqual([o['label'] for o in opts], ['merge now', 'wait for #55'])
        self.assertEqual(body, 'Merge DR-66?')
        a = ma.ask(['talk:1', 'decide:5:1'], 't', body, '', opts, {'type': 'talk'})
        self.assertIsNotNone(a)
        self.assertIsNone(ma.ask(['decide:5:1'], 't2', 'b'))
        self.assertIsNotNone(ma.ask(['decide:5:2'], 't3', 'b'))

    def row(self, aid):
        with ma.db() as c:
            return dict(c.execute('select * from asks where id=?', (aid,)).fetchone())

    def test_fallback_send_and_rate_limit(self):
        for i in range(7):
            ma.ask([f'k{i}'], f'title {i}', 'body', 'https://x', [{'label': 'yes'}], {'type': 'talk'})
        sent = []
        with mock.patch.object(ma, 'hermes_send', side_effect=sent.append), \
             mock.patch.object(ma, 'spanish', return_value=('¿Fusionar?', ['sí'])), \
             mock.patch.object(ma, '_cfg', return_value={'max_per_10min': 5}):
            with ma.db() as c:
                ma.send_pending(c, None); ma.send_pending(c, None)
        self.assertEqual(len(sent), 5)
        self.assertIn('Responde «P1 1»', sent[0])
        self.assertIn('https://x', sent[0])

    def test_bot_send_has_buttons_and_callback_routes(self):
        aid = ma.ask(['k'], 'DocReview #350 «X» · Ringo necesita una decisión', 'Merge?', '', [{'label': 'merge'}, {'label': 'wait'}],
                     {'type': 'deck', 'card': 350, 'board': 15, 'room': 'r', 'agent': 'ringo', 'title': 'X'})
        calls = []
        def tg(token, method, **p):
            calls.append((method, p))
            if method == 'sendMessage':
                return {'message_id': 77}
            if method == 'getUpdates':
                return [{'update_id': 5, 'callback_query': {'id': 'q', 'from': {'id': 123456789}, 'data': f'mq:{aid}:1',
                                                            'message': {'message_id': 77, 'text': 'x'}}}]
            return {}
        routed = []
        with mock.patch.object(ma, 'tg', side_effect=tg), \
             mock.patch.object(ma, 'spanish', return_value=('¿Fusionar?', ['fusionar', 'esperar'])), \
             mock.patch.object(ma, 'route', side_effect=lambda rt, row, ans, act: routed.append((ans, act)) or 'ok'):
            with ma.db() as c:
                ma.send_pending(c, 'TOKEN'); ma.read_bot(c, 'TOKEN')
                self.assertEqual(ma.meta(c, 'tg_offset'), '6')
        kb = calls[0][1]['reply_markup']['inline_keyboard']
        self.assertEqual([b[0]['callback_data'] for b in kb], [f'mq:{aid}:0', f'mq:{aid}:1'])
        self.assertEqual(routed, [('wait', 'relay')])
        self.assertEqual(self.row(aid)['status'], 'done')

    def test_strangers_are_ignored(self):
        aid = ma.ask(['k'], 't', 'b', '', [{'label': 'a'}])
        ups = [{'update_id': 1, 'callback_query': {'id': 'q', 'from': {'id': 1}, 'data': f'mq:{aid}:0'}},
               {'update_id': 2, 'message': {'message_id': 3, 'chat': {'id': 1}, 'text': f'P{aid} a'}}]
        with mock.patch.object(ma, 'tg', side_effect=lambda t, m, **p: ups if m == 'getUpdates' else {}), \
             mock.patch.object(ma, 'route') as rt:
            with ma.db() as c:
                ma.read_bot(c, 'TOKEN')
        rt.assert_not_called()

    def test_hermes_reply_is_read_once(self):
        aid = ma.ask(['k'], 't', 'b', '', [{'label': 'merge'}, {'label': 'wait'}], {'type': 'talk', 'room': 'r', 'mid': 9, 'agent': 'ringo'})
        hp = os.path.join(TMP, 'hermes.db')
        h = sqlite3.connect(hp)
        h.executescript('drop table if exists messages; drop table if exists sessions;'
                        'create table sessions(id text, source text); create table messages(id integer primary key, session_id text, role text, content text);'
                        "insert into sessions values('s','telegram'),('w','whatsapp'); insert into messages values(1,'s','user','old');")
        h.commit()
        relays, confirms = [], []
        with mock.patch.object(ma, 'relay', side_effect=lambda *a: relays.append(a)), \
             mock.patch.object(ma, 'confirm', side_effect=lambda tok, t, *a: confirms.append(t)):
            with ma.db() as c:
                ma.read_hermes(c, None, hp)                  # first run: start from now
                h.executescript(f"insert into messages values(2,'s','user','P{aid} 2'); insert into messages values(3,'s','user','P{aid} 2');"
                                f"insert into messages values(4,'w','user','P{aid} 1'); insert into messages values(5,'s','assistant','P{aid} 1');")
                h.commit()
                ma.read_hermes(c, None, hp)
        self.assertEqual(relays, [('r', 'ringo', 'wait', 9)])
        self.assertEqual(len(confirms), 1)

    def test_routes(self):
        posts = []
        sess = mock.Mock()
        sess.post.side_effect = lambda url, **kw: posts.append((url, kw)) or mock.Mock(status_code=201)
        with mock.patch.object(ma, 'nc', return_value=sess):
            row = {'body': 'Allow Muse to open x.com using Browser?'}
            self.assertIn('Permitir', ma.route({'type': 'muse', 'sha': 'abc'}, row, 'Permitir', 'allow'))
            self.assertEqual(json.load(open(os.path.join(ma.MUSE_DIR, 'abc.json')))['action'], 'allow')
            ma.route({'type': 'deck', 'card': 350, 'board': 15, 'room': 'r', 'agent': 'ringo', 'title': 'X', 'mid': 4},
                     {'body': 'Merge?'}, 'merge now', 'relay')
            ma.route({'type': 'talk', 'room': 'm', 'mid': 8, 'agent': 'muse'}, {'body': ''}, 'ok', 'relay')
        self.assertTrue(posts[0][0].endswith('/cards/350/comments'))
        self.assertTrue(posts[1][1]['data']['message'].startswith(ma.RELAY_PREFIX + ' @ringo sobre la tarjeta #350'))
        self.assertEqual(posts[1][1]['data']['replyTo'], 4)
        self.assertTrue(posts[2][1]['data']['message'].startswith(ma.RELAY_PREFIX + ' @oscar ok'))   # Muse's questions go to Oscar


if __name__ == '__main__':
    unittest.main()
