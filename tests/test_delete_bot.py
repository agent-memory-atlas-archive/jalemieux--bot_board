"""Deleting a bot (#280): from the board at once, from its box through its keeper, and from a
box whose keeper is offline once it reports again. Run with python -m unittest discover -s tests."""
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
from fastapi.testclient import TestClient       # noqa: E402
from app import db, main                         # noqa: E402
from test_fleet_kill import load_keeper          # noqa: E402

BOT = str(ROOT / "bin" / "bot")


class Board(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, 'board.db')
        db._local = threading.local()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.human = self.register('op', 'human')
        self.keeper = self.register('keeper-box', 'keeper')
        self.client.post('/api/workspaces', headers=self.human, json={'slug': 'proj', 'name': 'Proj', 'purpose': 'x'})
        self.bot = self.register('tarzan', 'claude-code', 'proj')
        r = self.client.post('/api/messages', headers=self.human, json={'channel': 'proj', 'body': 'a task'})
        self.tid = r.json()['message']['id']
        self.client.post(f'/api/threads/{self.tid}/worker', headers=self.bot).raise_for_status()
        self.client.post('/api/messages', headers=self.bot, json={'channel': 'proj', 'reply_to': self.tid,
                                                                  'body': 'on it'}).raise_for_status()
        self.client.put('/api/fleet/bots/tarzan', headers=self.human,
                        json={'host': 'box', 'run': True, 'harness': 'claude'})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, 'conn'):
            db._local.conn.close()
        self.tmp.cleanup()

    def register(self, handle, kind, workspace=None):
        r = self.client.post('/api/agents', json={'handle': handle, 'kind': kind, 'workspace': workspace})
        self.assertEqual(r.status_code, 201, r.text)
        return {'Authorization': 'Bearer ' + r.json()['token']}

    def report(self, status=None):
        bots = [{'handle': 'tarzan', 'harness': 'claude', 'status': status, 'serves': '/pool', 'pool': True}] if status else []
        r = self.client.put('/api/fleet/hosts/box', headers=self.keeper, json={'bots': bots})
        self.assertEqual(r.status_code, 200, r.text)
        return [w for w in r.json()['want'] if w['handle'] == 'tarzan']

    def delete(self, handle='tarzan', who=None, expected=200):
        r = self.client.delete(f'/api/agents/{handle}', headers=who or self.human)
        self.assertEqual(r.status_code, expected, r.text)

    def gone_from_the_board(self):
        self.assertEqual(self.client.get('/api/me', headers=self.bot).status_code, 401)
        self.assertNotIn('tarzan', [a['handle'] for a in self.client.get('/api/agents').json()['agents']])
        self.assertNotIn('"tarzan"', self.client.get('/').text)      # the @ suggestions are this roster
        self.assertEqual(self.client.get('/api/agents/tarzan').status_code, 404)
        self.assertEqual(self.client.get('/a/tarzan').status_code, 404)
        self.assertIsNone(self.client.get(f'/api/threads/{self.tid}/worker').json()['worker'])
        msgs = self.client.get(f'/api/threads/{self.tid}').json()['messages']
        self.assertEqual(msgs[1]['author'], 'tarzan')       # its old messages keep its handle
        self.assertEqual(msgs[-1]['meta'], {'kind': 'thread_released'})
        r = self.client.post('/api/agents', json={'handle': 'tarzan', 'kind': 'claude-code', 'workspace': 'proj'})
        self.assertEqual(r.status_code, 409)                 # not reused

    def test_a_running_bot(self):
        self.report('running proj')
        self.delete()
        self.gone_from_the_board()
        # its keeper is asked to stop it and remove it; /fleet says so until it has
        want = self.report('running proj, stopping')
        self.assertFalse(want[0]['run'])
        self.assertTrue(want[0]['deleted'])
        self.assertIn('deleted: stopping', self.client.get('/fleet').text)
        self.assertNotIn('class="act delete-bot"', self.client.get('/fleet').text)
        self.assertTrue(self.report('stopped')[0]['deleted'])
        self.assertIn('being removed from its box', self.client.get('/fleet').text)
        self.assertEqual(self.report(), [])                  # the box no longer has it
        self.assertEqual(self.client.get('/api/fleet').json()['hosts'][0]['bots'], [])
        self.assertEqual(self.report(), [])

    def test_a_bot_whose_keeper_is_offline(self):
        self.report('stopped')
        with db._write_lock, db.conn() as c:
            c.execute("UPDATE fleet_hosts SET reported_at='2020-01-01T00:00:00+00:00'")
        self.delete()
        self.gone_from_the_board()
        self.assertEqual(self.client.get('/api/fleet').json()['hosts'][0]['bots'], [])
        # the box comes back: its keeper is told, removes it, and the board forgets it
        self.assertTrue(self.report('stopped')[0]['deleted'])
        self.assertEqual(self.report(), [])

    def test_a_copy_on_another_box_is_asked_too(self):
        # #368: one row, on one box, but the other box reports a copy; it is asked when it next reports
        other = self.register('keeper-old', 'keeper')
        def old(*bots):
            r = self.client.put('/api/fleet/hosts/old', headers=other, json={'bots': [
                {'handle': h, 'harness': 'codex', 'status': s, 'serves': '/pool', 'pool': True} for h, s in bots]})
            self.assertEqual(r.status_code, 200, r.text)
            return r.json()['want']
        self.assertEqual(old(('tarzan', 'waiting')), [], 'not while the bot is alive')
        self.report('waiting')
        self.delete()
        self.assertTrue(self.report('waiting')[0]['deleted'])

        want = old(('tarzan', 'waiting'))
        self.assertEqual([(w['handle'], w['harness'], w['run'], bool(w['deleted'])) for w in want], [('tarzan', 'codex', False, True)])
        self.assertEqual(old(('tarzan', 'moved')), [], "a hand-over copy is the keeper's own")
        self.assertEqual(old(), [])

        # the row goes when its own box is done; a box that was dark is still asked when it returns
        self.assertEqual(self.report(), [])
        self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM fleet_bots WHERE handle='tarzan'").fetchone()[0], 0)
        self.assertTrue(old(('tarzan', 'stopped'))[0]['deleted'])

        # the keeper's part takes it as it takes a row (its reconcile is tested with one below)
        self.assertEqual({'handle', 'host', 'harness', 'run', 'set_by', 'set_at', 'deleted'} - set(want[0]), set())

    def test_a_copy_of_a_living_bot_or_a_retired_keeper_is_not_asked(self):
        other = self.register('keeper-old', 'keeper')
        self.register('keeper-gone', 'keeper')
        with db._write_lock, db.conn() as c:
            c.execute("UPDATE agents SET retired_at='2020-01-01T00:00:00+00:00' WHERE handle='keeper-gone'")
        r = self.client.put('/api/fleet/hosts/old', headers=other, json={'bots': [
            {'handle': h, 'harness': 'claude', 'status': 'stopped', 'serves': '/pool', 'pool': True} for h in ('tarzan', 'keeper-gone')]})
        self.assertEqual(r.json()['want'], [])

    def test_the_fleet_page_and_the_bot_page_offer_it(self):
        self.report('running proj')
        self.assertIn('class="act delete-bot" data-handle="tarzan"', self.client.get('/fleet').text)
        self.assertIn('id="delete-bot"', self.client.get('/a/tarzan').text)
        self.assertNotIn('id="delete-bot"', self.client.get('/a/op').text)

    def test_who_and_what_can_be_deleted(self):
        self.delete(who=self.bot, expected=403)
        self.delete('op', expected=409)
        self.delete('keeper-box', expected=409)
        self.delete('nobody', expected=404)
        self.delete()
        self.delete(expected=409)
        r = self.client.put('/api/fleet/bots/tarzan', headers=self.human, json={'host': 'box', 'run': True})
        self.assertEqual(r.status_code, 409)


class Keeper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = os.environ.get('HOME'), os.environ.get('BOT_CONFIG')
        try:
            self.keeper = load_keeper(self.tmp.name)
        finally:
            for k, v in zip(('HOME', 'BOT_CONFIG'), home):
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v

    def tearDown(self):
        self.tmp.cleanup()

    def test_stops_it_then_deletes_it_once(self):
        hands = self.keeper.Hands()
        spawned, ran = [], []
        hands.spawn = lambda h, cmd: spawned.append(cmd[1:])
        real = subprocess.run      # the keeper's subprocess is this one: put it back after
        self.keeper.subprocess.run = lambda cmd, **kw: ran.append(cmd[1:]) or subprocess.CompletedProcess(cmd, 0, '', '')
        try:
            want = [{'handle': 'tarzan', 'harness': 'claude', 'run': False, 'deleted': '2026-10-28T22:00:00Z',
                     'set_by': 'op', 'set_at': '2026-10-28T22:00:00Z', 'move_to': None, 'fetch': None}]
            hands.reconcile(want, [{'handle': 'tarzan', 'status': 'running proj'}])
            hands.reconcile(want, [{'handle': 'tarzan', 'status': 'running proj, stopping'}])
            self.assertEqual(spawned, [['stop', 'tarzan']])     # a plain stop: its run finishes first
            self.assertEqual(ran, [])
            hands.reconcile(want, [{'handle': 'tarzan', 'status': 'stopped'}])
            self.assertEqual(ran, [['delete', 'tarzan']])
            hands.reconcile(want, [])                            # gone: nothing more to do
            # a leftover (#368) has only these fields, and goes the same way
            ran.clear()
            left = [{'handle': 'tarzan', 'host': 'old', 'harness': 'codex', 'run': False, 'set_by': 'board',
                     'set_at': '2026-11-02T00:00:00+00:00', 'deleted': '2026-11-02T00:00:00+00:00'}]
            self.assertEqual(hands.reconcile(left, [{'handle': 'tarzan', 'status': 'stopped'}]), {})
            self.assertEqual(ran, [['delete', 'tarzan']])
        finally:
            self.keeper.subprocess.run = real


class Launcher(unittest.TestCase):
    def test_bot_delete_removes_what_the_bot_left_on_the_box(self):
        with tempfile.TemporaryDirectory() as home:
            h = Path(home)
            cfg = h / '.config' / 'bot_board'
            pool = h / 'pool' / 'tarzan'
            other = h / 'src' / 'jane'     # named like the bot, but a checkout it was given
            state = cfg / 'bots' / 'tarzan'
            for d in (state / 'old-sessions' / 'x', pool, other, cfg / 'bots' / 'jane'):
                d.mkdir(parents=True)
            (state / 'dir').write_text(f'{pool}\n')
            (state / 'session').write_text('11111111-aaaa\n')
            (state / 'old-sessions' / 'x' / 'session').write_text('22222222-bbbb\n')
            (cfg / 'tarzan.token').write_text('bb_x')
            (cfg / 'tarzan.seen').write_text('5')
            (cfg / 'jane.token').write_text('bb_y')
            proj = h / '.claude' / 'projects' / '-pool-tarzan'
            proj.mkdir(parents=True)
            for n in ('11111111-aaaa.jsonl', '22222222-bbbb.jsonl', 'someone-else.jsonl'):
                (proj / n).write_text('{}')
            env = {**os.environ, 'HOME': home, 'BOT_CONFIG': str(h / 'config')}
            r = subprocess.run([BOT, 'delete', 'tarzan'], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            for p in (state, cfg / 'tarzan.token', cfg / 'tarzan.seen', pool,
                      proj / '11111111-aaaa.jsonl', proj / '22222222-bbbb.jsonl'):
                self.assertFalse(p.exists(), p)
            for p in (cfg / 'jane.token', cfg / 'bots' / 'jane', proj / 'someone-else.jsonl'):
                self.assertTrue(p.exists(), p)
            # a repo bot's checkout is not its own to remove
            jane = cfg / 'bots' / 'jane'
            (jane / 'dir').write_text(f'{other}\n'); (jane / 'repo').write_text(f'{other}\n')
            r = subprocess.run([BOT, 'delete', 'jane'], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(jane.exists())
            self.assertTrue(other.exists())
            r = subprocess.run([BOT, 'delete', 'jane'], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, 'bot: no bot jane on this box'))

    def test_a_pool_directory_with_work_not_pushed_stays(self):
        with tempfile.TemporaryDirectory() as home:
            h = Path(home)
            cfg, pool = h / '.config' / 'bot_board', h / 'pool' / 'tarzan'
            state = cfg / 'bots' / 'tarzan'
            state.mkdir(parents=True)
            (state / 'dir').write_text(f'{pool}\n')
            env = {**os.environ, 'HOME': home, 'BOT_CONFIG': str(h / 'config'),
                   'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
            clone = pool / 'some-repo'      # a clone the bot made in its directory, as bots do
            clone.mkdir(parents=True)
            git = lambda *a: subprocess.run(['git', '-C', str(clone), *a], env=env, check=True, capture_output=True)
            git('init', '-q'); git('commit', '-q', '--allow-empty', '-m', 'x')     # a commit on no remote
            r = subprocess.run([BOT, 'delete', 'tarzan'], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn(f'bot: left {pool}: work not pushed in some-repo', r.stdout)
            self.assertTrue((clone / '.git').exists())
            self.assertFalse(state.exists(), 'the bot itself is gone all the same')


if __name__ == '__main__':
    unittest.main()
