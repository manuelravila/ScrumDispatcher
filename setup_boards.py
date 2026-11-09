"""One-off setup: add Ringo/Miku/Freddy labels and agent ACLs to the project Deck boards."""
import json, sys
sys.argv = ['x']
sys.path.insert(0, '/opt/company')
import importlib.util
spec = importlib.util.spec_from_file_location('d', '/opt/company/dispatcher.py')
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)

COLORS = {'Ringo': '8e44ad', 'Miku': 'e91e63', 'Freddy': 'e67e22'}
for project, p in d.CFG['projects'].items():
    for b in p['boards']:
        full = d.deck('oscar', 'GET', f'/boards/{b}')
        have = {l['title'] for l in full['labels']}
        acl = {a['participant']['uid'] for a in full.get('acl', [])}
        for t, c in COLORS.items():
            if t not in have:
                r = d.SESS['oscar'].post(f'{d.DECK}/boards/{b}/labels', json={'title': t, 'color': c}, timeout=30)
                print(b, 'label', t, r.status_code)
        for uid in ('claude', 'miku', 'freddy'):
            if uid in acl:
                continue
            body = {'type': 0, 'participant': uid, 'permissionEdit': True,
                    'permissionShare': False, 'permissionManage': False}
            r = d.SESS['oscar'].post(f'{d.DECK}/boards/{b}/acl', json=body, timeout=30)
            if r.status_code >= 300:
                r = d.SESS['ringo'].post(f'{d.DECK}/boards/{b}/acl', json=body, timeout=30)
            print(b, 'acl', uid, r.status_code)
