"""Workspaces: the isolation boundary, its channels, its bots, its knowledge base, and the
admin workspace that seeds the others. Run with python -m unittest discover -s tests."""
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import db, main, workspaces


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, 'board.db')
        db._local = threading.local()
        self.before_start()
        self.client = TestClient(main.app)
        self.client.__enter__()

    def before_start(self):
        pass

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, 'conn'):
            db._local.conn.close()
        self.tmp.cleanup()

    def register(self, handle, kind, workspace=None, expected=201):
        body = {'handle': handle, 'kind': kind}
        if workspace:
            body['workspace'] = workspace
        r = self.client.post('/api/agents', json=body)
        self.assertEqual(r.status_code, expected, r.text)
        return {'Authorization': 'Bearer ' + r.json()['token']} if expected == 201 else r.json()

    def call(self, method, path, data=None, auth=None, expected=200):
        r = self.client.request(method, path, json=data, headers=auth)
        self.assertEqual(r.status_code, expected, r.text)
        return r.json()

    def post(self, auth, body, channel=None, reply_to=None, expected=201, meta=None):
        data = {'body': body, 'meta': meta or {}}
        if channel:
            data['channel'] = channel
        if reply_to:
            data['reply_to'] = reply_to
        return self.call('POST', '/api/messages', data, auth=auth, expected=expected)


class Workspaces(Base):
    def setUp(self):
        super().setUp()
        self.human = self.register('jac', 'human')
        self.game = self.call('POST', '/api/workspaces', {'slug': 'game', 'name': 'Space Game', 'purpose': 'A game',
                              'seed': {'description': 'A space sandbox.', 'links': ['https://example.com/design'],
                                       'text': 'Factions: the Lorre and the Vane.'}}, auth=self.human, expected=201)['workspace']
        self.call('POST', '/api/workspaces', {'slug': 'other', 'name': 'Other', 'purpose': 'Something else'},
                  auth=self.human, expected=201)
        self.ada = self.register('ada', 'claude-code', 'game')
        self.milo = self.register('milo', 'codex', 'other')
        self.root = self.register('root', 'claude-code', 'admin')

    def test_a_workspace_comes_with_general_and_a_seed_thread_for_the_admin_bots(self):
        self.assertEqual(self.game['channel'], 'game')
        self.assertEqual([c['slug'] for c in self.game['channels']], ['game'])
        self.assertTrue(self.game['channels'][0]['home'])
        self.assertEqual(self.game['channels'][0]['name'], 'General')
        tid = self.game['seed_thread_id']
        self.assertTrue(tid)
        seed = self.call('GET', f'/api/messages/{tid}', auth=self.human)['message']
        self.assertEqual(seed['channel'], 'admin')
        self.assertEqual(seed['author'], 'jac')
        self.assertEqual(seed['meta']['kind'], 'seed')
        self.assertEqual(seed['meta']['workspace'], 'game')
        self.assertIsNone(seed['meta']['channel'])
        self.assertIn('https://example.com/design', seed['body'])
        self.assertIn('A space sandbox.', seed['body'])
        self.assertEqual(self.client.get(seed['attachments'][0]['url'], headers=self.root).text,
                         'Factions: the Lorre and the Vane.')
        # the admin bot sees it as open work; a game bot never does
        self.assertIn(tid, [m['id'] for m in self.call('GET', '/api/messages', auth=self.root)['messages']])
        self.assertNotIn(tid, [m['id'] for m in self.call('GET', '/api/messages', auth=self.ada)['messages']])
        self.call('POST', f'/api/threads/{tid}/worker', auth=self.root)
        # a workspace without seed material posts nothing
        other = self.call('GET', '/api/workspaces/other', auth=self.human)['workspace']
        self.assertIsNone(other['seed_thread_id'])
        # the seed kind is the board's own
        self.post(self.human, 'fake seed', channel='admin', meta={'kind': 'seed'}, expected=403)

    def test_a_bot_sees_only_its_workspace(self):
        g = self.post(self.human, 'game work', channel='game')['message']
        o = self.post(self.human, 'other work', channel='other')['message']
        self.assertEqual([c['slug'] for c in self.call('GET', '/api/channels', auth=self.ada)['channels']], ['game'])
        self.assertEqual([m['id'] for m in self.call('GET', '/api/messages', auth=self.ada)['messages']], [g['id']])
        self.assertEqual([m['id'] for m in self.call('GET', '/api/messages', auth=self.milo)['messages']], [o['id']])
        self.call('GET', f"/api/messages/{o['id']}", auth=self.ada, expected=404)
        self.call('GET', f"/api/threads/{o['id']}", auth=self.ada, expected=404)
        self.call('GET', '/api/channels/other', auth=self.ada, expected=404)
        self.post(self.ada, 'sneaking in', channel='other', expected=403)
        self.call('POST', f"/api/threads/{o['id']}/worker", auth=self.ada, expected=403)
        self.call('POST', f"/api/threads/{g['id']}/worker", auth=self.ada)
        # search and the roster are scoped too; humans see everything
        self.assertEqual([m['id'] for m in self.call('GET', '/api/search?q=work', auth=self.ada)['messages']], [g['id']])
        self.assertEqual({a['handle'] for a in self.call('GET', '/api/agents', auth=self.ada)['agents']}, {'ada', 'jac'})
        self.assertEqual(len(self.call('GET', '/api/messages', auth=self.human)['messages']), 3)   # + the seed thread
        self.assertEqual([m['id'] for m in self.call('GET', '/api/messages?workspace=other', auth=self.human)['messages']], [o['id']])
        # a bot that names no channel posts in its General; a channel it names that does not exist is created there
        home = self.post(self.ada, 'hello')['message']
        self.assertEqual(home['channel'], 'game')
        self.post(self.ada, 'a new place', channel='game.scratch')
        self.assertEqual(self.call('GET', '/api/channels/game.scratch', auth=self.ada)['channel']['workspace'], 'game')
        self.call('GET', '/api/channels/game.scratch', auth=self.milo, expected=404)
        # a mention from another workspace does not reach the inbox
        self.post(self.human, '@ada look', channel='other')
        self.assertEqual([m['body'] for m in self.call('GET', '/api/inbox', auth=self.ada)['messages']], ['game work'])
        me = self.call('GET', '/api/me', auth=self.ada)
        self.assertEqual(me['workspace']['slug'], 'game')
        self.assertFalse(me['admin_bot'])
        self.assertTrue(self.call('GET', '/api/me', auth=self.root)['admin_bot'])

    def test_channels_carry_focus_and_references_that_only_humans_edit(self):
        ch = self.call('POST', '/api/workspaces/game/channels', {
            'slug': 'lore', 'name': 'Art & Lore', 'description': 'Factions and visuals',
            'focus': 'Keep factions consistent with lore/factions.md.',
            'references': [{'path': 'lore/INDEX.md', 'mandatory': True}, {'path': 'lore/factions.md'}]},
            auth=self.human, expected=201)['channel']
        self.assertEqual(ch['slug'], 'game.lore')
        got = self.call('GET', '/api/channels/game.lore', auth=self.ada)['channel']
        self.assertEqual(got['name'], 'Art & Lore')
        self.assertEqual(got['focus'], 'Keep factions consistent with lore/factions.md.')
        self.assertEqual(got['references'], [{'path': 'lore/INDEX.md', 'mandatory': True},
                                             {'path': 'lore/factions.md', 'mandatory': False}])
        self.assertEqual(got['workspace'], 'game')
        self.call('PATCH', '/api/workspaces/game/channels/lore', {'focus': 'changed'}, auth=self.ada, expected=403)
        self.call('PATCH', '/api/workspaces/game/channels/lore', {'focus': 'changed'}, auth=self.root, expected=403)
        self.call('PATCH', '/api/workspaces/game/channels/lore', {'focus': 'changed', 'references': []}, auth=self.human)
        self.assertEqual(self.call('GET', '/api/channels/game.lore', auth=self.ada)['channel']['focus'], 'changed')
        self.call('POST', '/api/workspaces/game/channels', {'slug': 'x', 'name': 'X', 'references': [{'path': '../etc'}]},
                  auth=self.human, expected=422)
        listed = {c['slug']: c for c in self.call('GET', '/api/workspaces/game', auth=self.human)['workspace']['channels']}
        self.assertEqual(set(listed), {'game', 'game.lore'})
        # a channel can be seeded too: the thread names it
        seeded = self.call('POST', '/api/workspaces/game/channels', {'slug': 'economy', 'name': 'Economy',
                           'seed': {'links': ['https://example.com/economy']}}, auth=self.human, expected=201)['channel']
        seed = self.call('GET', f"/api/messages/{seeded['seed_thread_id']}", auth=self.root)['message']
        self.assertEqual((seed['meta']['workspace'], seed['meta']['channel']), ('game', 'game.economy'))

    def test_the_moderator_reads_posts_and_mentions_everywhere_but_claims_only_in_admin(self):
        # #319: a game thread is invisible to another workspace's bot, and the moderator's to read
        tid = self.post(self.ada, 'economy question', channel='game')['message']['id']
        self.call('GET', f'/api/messages/{tid}', auth=self.milo, expected=404)
        self.assertEqual(self.call('GET', f'/api/threads/{tid}', auth=self.root)['messages'][0]['id'], tid)
        ids = lambda r: [m['id'] for m in r['messages']]
        self.assertIn(tid, ids(self.call('GET', '/api/messages', auth=self.root)))
        self.assertIn(tid, ids(self.call('GET', '/api/messages?workspace=game', auth=self.root)))
        self.assertNotIn(tid, ids(self.call('GET', '/api/messages?workspace=other', auth=self.root)))
        self.assertIn(tid, ids(self.call('GET', '/api/search?q=economy', auth=self.root)))
        self.assertIn('game', {c['slug'] for c in self.call('GET', '/api/channels', auth=self.root)['channels']})
        # it replies in the game channel, and the game bot is reached there
        reply = self.post(self.root, '@ada milo can review this', channel='game', reply_to=tid)
        self.assertNotIn('warnings', reply)
        self.assertIn(reply['message']['id'], ids(self.call('GET', '/api/inbox', auth=self.ada)))
        # a mention from the admin channel does not reach a game bot, and the sender is told
        warned = self.post(self.root, '@ada please look at the admin thread', channel='admin')
        self.assertEqual([(w['mention'], w['state']) for w in warned['warnings']], [('ada', 'elsewhere')])
        self.assertNotIn(warned['message']['id'], ids(self.call('GET', '/api/inbox', auth=self.ada)))
        self.assertNotIn('warnings', self.post(self.human, '@root a sweep please', channel='game'))
        # it claims nothing outside admin; the thread stays for the workspace's bots
        self.call('POST', f'/api/threads/{tid}/worker', auth=self.root, expected=403)
        self.call('POST', f'/api/threads/{tid}/worker', auth=self.ada)
        admin_tid = self.post(self.human, 'moderator, a sweep please', channel='admin')['message']['id']
        self.call('POST', f'/api/threads/{admin_tid}/worker', auth=self.root)
        # an ordinary bot is as scoped as before
        self.post(self.milo, 'hi', channel='game', expected=403)
        self.assertFalse(self.call('GET', '/api/me', auth=self.milo)['admin_bot'])
        self.assertTrue(self.call('GET', '/api/me', auth=self.root)['admin_bot'])

    def test_a_channel_the_moderator_creates_lands_in_admin_unless_it_names_a_workspace(self):
        admin = self.call('GET', '/api/workspaces/admin', auth=self.human)['workspace']['id']
        slug_workspace = lambda slug: db.get_board(slug)['workspace_id']
        self.post(self.root, 'hello', channel='admin-new')
        self.assertEqual(slug_workspace('admin-new'), admin)
        self.call('POST', '/api/channels', {'slug': 'admin-other', 'topic': 't'}, auth=self.root, expected=201)
        self.assertEqual(slug_workspace('admin-other'), admin)
        self.call('POST', '/api/channels', {'slug': 'game-new', 'topic': 't', 'workspace': 'game'}, auth=self.root, expected=201)
        self.assertEqual(slug_workspace('game-new'), self.game['id'])
        # a human's still has no workspace
        self.post(self.human, 'hello', channel='human-new')
        self.assertIsNone(slug_workspace('human-new'))

    def test_knowledge_is_revisioned_scoped_and_shared_with_admin_bots(self):
        doc = self.call('PUT', '/api/knowledge/INDEX.md', {'body': '# Game\n\n- lore/factions.md', 'base_revision': 0,
                        'reason': 'first index', 'sources': ['12']}, auth=self.ada)['document']
        self.assertEqual((doc['path'], doc['revision'], doc['author']), ('INDEX.md', 1, 'ada'))
        self.call('PUT', '/api/knowledge/INDEX.md', {'body': 'stale write', 'base_revision': 0}, auth=self.ada, expected=409)
        doc = self.call('PUT', '/api/knowledge/INDEX.md', {'body': '# Game v2\n\n- lore/factions.md', 'base_revision': 1,
                        'reason': 'tightened'}, auth=self.ada)['document']
        self.assertEqual(doc['revision'], 2)
        self.call('PUT', '/api/knowledge/lore/factions.md', {'body': 'The Lorre are traders.', 'base_revision': 0}, auth=self.ada)
        self.assertEqual(self.call('GET', '/api/knowledge/INDEX.md', auth=self.ada)['document']['body'], '# Game v2\n\n- lore/factions.md')
        self.assertEqual(self.call('GET', '/api/knowledge/INDEX.md?revision=1', auth=self.ada)['document']['body'], '# Game\n\n- lore/factions.md')
        hist = self.call('GET', '/api/knowledge/INDEX.md/history', auth=self.ada)
        self.assertEqual([r['revision'] for r in hist['revisions']], [2, 1])
        self.assertEqual([d['path'] for d in self.call('GET', '/api/knowledge', auth=self.ada)['documents']], ['INDEX.md', 'lore/factions.md'])
        found = self.call('GET', '/api/knowledge?q=traders', auth=self.ada)['documents']
        self.assertEqual([d['path'] for d in found], ['lore/factions.md'])
        self.assertIn('traders', found[0]['snippet'])
        # another workspace's bot has its own, empty, knowledge base
        self.assertEqual(self.call('GET', '/api/knowledge', auth=self.milo)['documents'], [])
        self.call('GET', '/api/knowledge/INDEX.md', auth=self.milo, expected=404)
        self.call('GET', '/api/workspaces/game/knowledge', auth=self.milo, expected=403)
        # admin bots and humans reach it by name
        self.assertEqual(len(self.call('GET', '/api/workspaces/game/knowledge', auth=self.root)['documents']), 2)
        self.call('PUT', '/api/workspaces/game/knowledge/decisions/decisions.md',
                  {'body': 'Teal and rust palette.', 'base_revision': 0, 'reason': 'seeded'}, auth=self.root)
        self.assertEqual(self.call('GET', '/api/knowledge/decisions/decisions.md', auth=self.ada)['document']['author'], 'root')
        self.assertEqual(self.call('GET', '/api/workspaces/game/knowledge/INDEX.md', auth=self.human)['document']['revision'], 2)
        # delete keeps history
        self.call('DELETE', '/api/knowledge/lore/factions.md', {'base_revision': 1, 'reason': 'merged'}, auth=self.ada)
        self.call('GET', '/api/knowledge/lore/factions.md', auth=self.ada, expected=404)
        self.assertEqual(len(self.call('GET', '/api/knowledge/lore/factions.md/history', auth=self.ada)['revisions']), 2)
        self.assertEqual([d['path'] for d in self.call('GET', '/api/knowledge', auth=self.ada)['documents']], ['INDEX.md', 'decisions/decisions.md'])
        self.call('PUT', '/api/knowledge/bad path', {'body': 'x', 'base_revision': 0}, auth=self.ada, expected=422)
        self.call('PUT', '/api/knowledge/empty.md', {'body': '  ', 'base_revision': 0}, auth=self.ada, expected=422)
        self.assertEqual(self.call('GET', '/api/workspaces/game', auth=self.human)['workspace']['documents'], 2)

    def test_the_guide_depends_on_who_asks(self):
        self.assertIn('fleet message board', self.client.get('/agents.md', headers=self.ada).text)
        self.assertIn('Board administration', self.client.get('/agents.md', headers=self.root).text)
        self.assertIn('fleet message board', self.client.get('/agents.md').text)
        self.assertEqual(self.client.get('/admin/agents.md', headers=self.ada).status_code, 403)
        self.assertEqual(self.client.get('/admin/agents.md', headers=self.root).status_code, 200)
        api = self.call('GET', '/api', auth=self.root)
        self.assertIn('administration', api)
        self.assertNotIn('administration', self.call('GET', '/api', auth=self.ada))
        schema = self.call('GET', '/api/openapi.json')
        self.assertIn('/api/knowledge/{path}', schema['paths'])
        self.assertNotIn('/api/workspaces', schema['paths'])

    def test_bots_belong_to_one_workspace_which_humans_set(self):
        self.assertEqual(self.call('GET', '/api/workspaces/game', auth=self.human)['workspace']['bots'], ['ada'])
        self.call('PUT', '/api/workspaces/game/bots', {'bots': ['ada', 'milo']}, auth=self.human)
        self.assertEqual(self.call('GET', '/api/me', auth=self.milo)['workspace']['slug'], 'game')
        self.assertEqual(self.call('GET', '/api/workspaces/other', auth=self.human)['workspace']['bots'], [])
        self.call('PUT', '/api/workspaces/game/bots', {'bots': ['jac']}, auth=self.human, expected=422)
        self.call('PUT', '/api/workspaces/game/bots', {'bots': ['ada']}, auth=self.ada, expected=403)
        self.call('GET', '/api/workspaces', auth=self.ada, expected=403)
        self.assertEqual({w['slug'] for w in self.call('GET', '/api/workspaces', auth=self.root)['workspaces']}, {'admin', 'game', 'other'})
        self.register('nobody', 'claude-code', 'nowhere', expected=404)
        # a bot of no workspace (a direct client, or one from before) sees the board as before
        lost = self.register('lost', 'claude-code')
        self.assertIsNone(self.call('GET', '/api/me', auth=lost)['workspace'])
        self.assertIn('game', [c['slug'] for c in self.call('GET', '/api/channels', auth=lost)['channels']])
        self.post(lost, 'anyone?', channel='game')
        self.post(lost, 'anyone?', expected=422)
        self.call('GET', '/api/knowledge', auth=lost, expected=400)

    def test_archiving_a_workspace_releases_its_bots(self):
        self.call('PATCH', '/api/workspaces/other', {'archived': True}, auth=self.human, expected=422)
        self.call('PATCH', '/api/workspaces/other', {'archived': True, 'archive_confirm': True}, auth=self.human)
        self.post(self.human, 'too late', channel='other', expected=403)
        self.assertTrue(self.call('GET', '/api/workspaces/other', auth=self.human)['workspace']['archived'])
        self.register('late', 'claude-code', 'other', expected=409)


class FleetWorkspace(Base):
    """A bot created from /fleet belongs to the workspace picked there."""

    def setUp(self):
        super().setUp()
        self.human = self.register('op', 'human')
        self.keeper = self.register('keeper-box', 'keeper')
        self.call('POST', '/api/workspaces', {'slug': 'falco', 'name': 'Falco', 'purpose': 'Bugs'}, auth=self.human, expected=201)
        self.report(bots=[])

    def report(self, bots):
        r = self.client.put('/api/fleet/hosts/box', headers=self.keeper, json={'bots': bots})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_new_bot_needs_a_workspace_and_the_keeper_is_told_it(self):
        self.call('POST', '/api/fleet/bots', {'host': 'box', 'harness': 'claude'}, auth=self.human, expected=422)
        self.call('POST', '/api/fleet/bots', {'host': 'box', 'harness': 'claude', 'workspace': 'ghost'}, auth=self.human, expected=409)
        handle = self.call('POST', '/api/fleet/bots', {'host': 'box', 'harness': 'claude', 'workspace': 'falco'}, auth=self.human)['handle']
        self.call('PUT', '/api/fleet/bots/newbie', {'host': 'box', 'run': True, 'harness': 'claude'}, auth=self.human, expected=409)
        want = self.report(bots=[])['want']
        self.assertEqual([(w['handle'], w['workspace']) for w in want], [(handle, 'falco')])
        # bin/bot registers with that workspace, as the keeper passes --workspace
        bot = self.register(handle, 'claude-code', 'falco')
        self.assertEqual(self.call('GET', '/api/me', auth=bot)['workspace']['slug'], 'falco')
        self.report(bots=[{'handle': handle, 'harness': 'claude', 'status': 'waiting', 'pool': True}])
        page = self.client.get('/fleet').text
        self.assertIn('href="/workspaces/falco"', page)
        self.assertEqual(self.call('GET', '/api/workspaces/falco', auth=self.human)['workspace']['bots'], [handle])

    def test_a_bot_that_registered_without_a_workspace_joins_it_when_its_box_reports(self):
        handle = self.call('POST', '/api/fleet/bots', {'host': 'box', 'harness': 'claude', 'workspace': 'falco'}, auth=self.human)['handle']
        bot = self.register(handle, 'claude-code')
        self.assertIsNone(self.call('GET', '/api/me', auth=bot)['workspace'])
        self.report(bots=[{'handle': handle, 'harness': 'claude', 'status': 'waiting', 'pool': True}])
        self.assertEqual(self.call('GET', '/api/me', auth=bot)['workspace']['slug'], 'falco')


class Migration(Base):
    """A board from before: projects, focus groups, a bot pool and published context become
    workspaces, channels, bot assignments and knowledge documents, in place."""

    def before_start(self):
        c = sqlite3.connect(db.DB_PATH)
        c.executescript(db.SCHEMA.replace(",\n    workspace_id INTEGER\n);", "\n);", 1).replace(
            ",\n    workspace_id INTEGER,\n    name       TEXT NOT NULL DEFAULT '',\n    focus      TEXT NOT NULL DEFAULT '',\n"
            "    refs       TEXT NOT NULL DEFAULT '[]',\n    seed_thread_id INTEGER,\n    archived   INTEGER NOT NULL DEFAULT 0\n);", "\n);", 1)
            .replace("CREATE INDEX IF NOT EXISTS idx_boards_workspace ON boards(workspace_id, kind);", ""))
        c.executescript("""
        CREATE TABLE project_admins (agent_id INTEGER PRIMARY KEY, granted_at TEXT NOT NULL);
        CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE COLLATE NOCASE, name TEXT NOT NULL,
            purpose TEXT NOT NULL, board_id INTEGER NOT NULL UNIQUE, owner_id INTEGER NOT NULL,
            repositories TEXT NOT NULL DEFAULT '[]', operators TEXT NOT NULL DEFAULT '[]',
            archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE project_groups (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, slug TEXT NOT NULL,
            name TEXT NOT NULL, purpose TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', related_groups TEXT NOT NULL DEFAULT '[]',
            board_id INTEGER NOT NULL UNIQUE, archived INTEGER NOT NULL DEFAULT 0, seed_thread_id INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE context_revisions (id INTEGER PRIMARY KEY, board_id INTEGER NOT NULL, base_revision INTEGER NOT NULL,
            revision INTEGER, status TEXT NOT NULL, body TEXT NOT NULL, sources TEXT NOT NULL, reason TEXT NOT NULL,
            author_id INTEGER NOT NULL, approved_by INTEGER, created_at TEXT NOT NULL, published_at TEXT);
        CREATE TABLE project_pools (project_id INTEGER PRIMARY KEY, configured INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE project_pool_bots (agent_id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL);
        INSERT INTO agents (id, handle, token_hash, kind, created_at) VALUES (1, 'jac', 'h1', 'human', 't'), (2, 'ada', 'h2', 'claude-code', 't'), (3, 'free', 'h3', 'codex', 't');
        INSERT INTO project_admins VALUES (1, 't');
        INSERT INTO boards (id, slug, topic, kind, created_at) VALUES (1, 'game', 'the game', 'channel', 't'), (2, 'game.art', 'art', 'channel', 't'),
            (3, 'lobby', '', 'channel', 't'), (4, 'help', '', 'channel', 't');
        INSERT INTO projects VALUES (1, 'game', 'Game', 'A game', 1, 1, '[]', '[]', 0, 't', 't');
        INSERT INTO project_groups VALUES (1, 1, 'art', 'Art', 'Consistent art', 'Assets and style', '[]', 2, 0, NULL, 't', 't');
        INSERT INTO context_revisions VALUES (1, 1, 0, 1, 'published', 'Project context v1', '["a"]', 'r', 1, 1, 't', 't'),
            (2, 1, 1, 2, 'published', 'Project context v2', '["b"]', 'r', 1, 1, 't', 't'),
            (3, 2, 0, 1, 'published', 'Palette: teal and rust.', '["c"]', 'r', 2, 1, 't', 't'),
            (4, 2, 1, NULL, 'draft', 'a draft', '[]', 'r', 2, NULL, 't', NULL);
        INSERT INTO project_pools VALUES (1, 1, 1);
        INSERT INTO project_pool_bots VALUES (2, 1);
        """)
        c.commit()
        c.close()

    def test_projects_groups_pools_and_context_become_workspaces(self):
        c = db.conn()
        names = {r['name'] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('workspaces', names)
        self.assertNotIn('projects', names)
        self.assertNotIn('project_groups', names)
        ws = workspaces.find('game')
        self.assertEqual(workspaces.home_slug(ws), 'game')
        chans = {ch['slug']: ch for ch in workspaces.channels_of(ws)}
        self.assertEqual(chans['game']['name'], 'General')
        self.assertEqual(chans['game.art']['name'], 'Art')
        self.assertEqual(chans['game.art']['focus'], 'Consistent art\n\nAssets and style')
        self.assertEqual(chans['game.art']['references'], [{'path': 'art/INDEX.md', 'mandatory': False}])
        self.assertEqual(db.get_agent_by_handle('ada')['workspace_id'], ws['id'])
        self.assertIsNone(db.get_agent_by_handle('free')['workspace_id'])
        self.assertEqual(workspaces.kb_get(ws, 'INDEX.md')['body'], 'Project context v2')
        self.assertEqual(workspaces.kb_get(ws, 'art/INDEX.md')['body'], 'Palette: teal and rust.')
        self.assertEqual([d['path'] for d in workspaces.kb_list(ws)['documents']], ['INDEX.md', 'art/INDEX.md'])
        # fleet channels are left as they were, attached to no workspace: no bot sees them
        self.assertIsNone(db.get_board('lobby')['workspace_id'])
        self.assertTrue(workspaces.is_admin(db.get_agent_by_handle('jac')))
        # the admin workspace was created for the admin that was there
        self.assertEqual(workspaces.kb_get(workspaces.find('admin'), 'INDEX.md')['revision'], 1)
        # a second start changes nothing
        db.init()
        self.assertEqual(len(workspaces.kb_history(ws, 'INDEX.md')['revisions']), 1)


if __name__ == '__main__':
    unittest.main()
