"""#83: the keeper checks credentials before starting bots, holds them while a login is
missing, and writes the board's secrets to fleet.env. Run with python -m unittest discover -s tests."""
import importlib.machinery
import importlib.util
import os
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from app import db, main


def load_keeper(home, **env):
    """bin/keeper with a scratch home, so it never reads or writes the real ~/.config/bot_board."""
    saved = {k: os.environ.get(k) for k in ('HOME', 'BOT_CONFIG', *env)}
    os.environ.update(HOME=home, BOT_CONFIG=os.path.join(home, 'config'), **env)
    try:
        loader = importlib.machinery.SourceFileLoader('keeper', str(ROOT / 'bin' / 'keeper'))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader('keeper', loader))
        loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
    return mod


def fake(bindir, name, script):
    p = os.path.join(bindir, name)
    with open(p, 'w') as f:
        f.write('#!/bin/sh\n' + script + '\n')
    os.chmod(p, 0o755)


class Keeper(unittest.TestCase):
    def setUp(self):
        # the developer's own GH_TOKEN (anyone who uses gh) must not stand in for the fleet's
        env = patch.dict(os.environ); env.start(); self.addCleanup(env.stop)
        os.environ.pop('GH_TOKEN', None)
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.bin = os.path.join(self.home, 'bin'); os.makedirs(self.bin)
        os.makedirs(os.path.join(self.home, '.config', 'bot_board'))
        # gh is happy only with the token from fleet.env; claude says whether a login file exists
        fake(self.bin, 'gh', '[ "$GH_TOKEN" = good ] && exit 0; '
                             '[ -n "$GH_TOKEN" ] && echo "The token in GH_TOKEN is invalid." >&2 && exit 1; '
                             'echo "You are not logged into any GitHub hosts." >&2; exit 1')
        fake(self.bin, 'claude', f'[ -e {self.home}/claude-login ] && echo \'{{"loggedIn": true}}\' '
                                 '|| echo \'{"loggedIn": false}\'')
        self.k = load_keeper(self.home)
        self.local = os.path.join(self.home, '.config', 'bot_board', 'fleet.env')
        self.board = {'secrets': {}, 'version': 'v0'}     # what GET /api/fleet/secrets/values answers
        self.fetches = 0
        def call(method, path, body, token=None):
            self.fetches += 1
            return (200, self.board) if token == 'keeper-token' else (403, {'detail': 'not a keeper'})
        self.k.call = call
        self.path = os.environ['PATH']
        os.environ['PATH'] = self.bin + os.pathsep + self.path

    def tearDown(self):
        os.environ['PATH'] = self.path

    def test_out_returns_a_commands_stdout(self):
        # the host name, memory and bot list all read through out(); it must give text, "" on failure
        self.assertEqual(self.k.out('echo', 'hi'), 'hi\n')
        self.assertEqual(self.k.out('no-such-command-here'), '')
        self.assertIsInstance(self.k.snapshot()['bots'], list)

    def test_the_runtime_setting_is_checked_and_reported(self):
        # #479: BOT_RUNTIME in the config; host when unset, the command otherwise, re-checked when it changes
        cfg = os.path.join(self.home, '.config', 'bot_board', 'config')
        def runtime(value):
            with open(cfg, 'w') as f:
                f.write(f'BOT_RUNTIME={value}\n' if value is not None else '')
            return self.k.runtime_state()
        self.assertEqual(runtime(None), {'setting': 'host', 'state': 'ok', 'detail': ''})
        self.assertEqual(runtime('host')['setting'], 'host')
        self.assertEqual(runtime('nerdctl'), {'setting': 'nerdctl', 'state': 'missing', 'detail': "no nerdctl on this box's PATH"})
        fake(self.bin, 'docker', '[ "$1" = version ] && echo 29.7.2')
        self.assertEqual(runtime('docker'), {'setting': 'docker', 'state': 'ok', 'detail': '29.7.2'})
        self.assertEqual(self.k.snapshot()['runtime']['detail'], '29.7.2')
        fake(self.bin, 'colima-docker', 'echo "Cannot connect to the Docker daemon" >&2; exit 1')
        got = runtime(os.path.join(self.bin, 'colima-docker'))
        self.assertEqual((got['state'], got['detail']), ('down', 'Cannot connect to the Docker daemon'))
        fake(self.bin, 'container', '[ "$1 $2" = "system status" ] && echo "apiserver is running"')
        self.assertEqual(runtime('container'), {'setting': 'container', 'state': 'ok', 'detail': 'apiserver is running'})
        self.tmp.cleanup()

    def creds(self, c=None, version=None, token='keeper-token'):
        c = c or self.k.Credentials('box')
        if version:
            c.sync(version, token)
        c.refresh({'claude'}, force=True)
        return c

    def secrets(self, version, **values):
        self.board = {'secrets': values, 'version': version}
        return version

    def want(self, h='tarzan'):
        return {'handle': h, 'harness': 'claude', 'run': True, 'set_by': 'op', 'set_at': '2026-10-15T01:00:00Z',
                'move_to': None, 'fetch': None, 'kill': None}

    def test_no_login_holds_the_bot_and_says_why(self):
        c = self.creds()
        self.assertEqual(c.state['claude'][0], 'missing')
        hands = self.k.Hands('box')
        spawned = []
        hands.spawn = lambda h, cmd: spawned.append(cmd)
        hands.blocked = c.blocked()
        problems = hands.reconcile([self.want()], [])
        self.assertEqual(spawned, [])
        self.assertEqual(problems['tarzan'], 'held: claude not logged in')
        row = next(r for r in c.report(hands.held) if r['name'] == 'claude')
        self.assertEqual((row['state'], row['held']), ('missing', 1))
        # the login comes back: the next round starts it, with no give-up left over
        open(os.path.join(self.home, 'claude-login'), 'w').close()
        hands.blocked = self.creds().blocked()
        self.assertEqual(hands.reconcile([self.want()], []), {})
        self.assertEqual(len(spawned), 1)

    def fleet_claude(self):
        """claude as it behaves with CLAUDE_CODE_OAUTH_TOKEN (#283): auth status says logged in for
        any token; a real request (-p) works for 'good', 401s for 'bad', and cannot tell for 'slow'."""
        fake(self.bin, 'claude', f'echo "$*" >> {self.home}/claude-calls\n'
             'if [ "$1" = auth ]; then [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ] && echo \'{"loggedIn": true}\' '
             '|| echo \'{"loggedIn": false}\'; exit 0; fi\n'
             'case $CLAUDE_CODE_OAUTH_TOKEN in good) echo \'{"is_error": false, "result": "OK."}\' ;;\n'
             ' bad) echo \'{"is_error": true, "api_error_status": 401, '
             '"result": "Failed to authenticate. API Error: 401 OAuth access token is invalid."}\' ;;\n'
             ' *) echo \'{"is_error": true, "api_error_status": 529, "result": "Overloaded"}\' ;; esac')

    def tries(self):
        try:
            return sum(1 for line in open(os.path.join(self.home, 'claude-calls')) if line.startswith('-p'))
        except FileNotFoundError:
            return 0

    def test_the_fleet_claude_token_is_tried_for_real(self):
        self.fleet_claude()
        # no token: not logged in, and no real request
        c = self.creds()
        self.assertEqual(c.state['claude'][0], 'missing')
        self.assertEqual(self.tries(), 0)
        # a good one: logged in, tried once, then not again within a day
        c = self.creds(c, self.secrets('v1', CLAUDE_CODE_OAUTH_TOKEN='good'))
        self.assertEqual(c.state['claude'], ('ok', ''))
        self.creds(c); self.creds(c)
        self.assertEqual(self.tries(), 1)
        self.assertEqual(c.blocked(), {})
        # rotated to a bad one: tried at once, rejected, and its bots are held
        c = self.creds(c, self.secrets('v2', CLAUDE_CODE_OAUTH_TOKEN='bad'))
        self.assertEqual(self.tries(), 2)
        self.assertEqual(c.state['claude'][0], 'rejected')
        self.assertIn('401', c.state['claude'][1])
        self.assertEqual(c.blocked(), {'claude': 'claude token rejected: Failed to authenticate. '
                                                 'API Error: 401 OAuth access token is invalid.'})
        self.assertNotIn("'bad'", str(c.report({})))    # the token never goes into the report
        # a rejected token is tried again in an hour, not a day: a 401 can be passing
        self.creds(c)
        self.assertEqual(self.tries(), 2)
        c.probe['at'] -= 3600
        self.creds(c)
        self.assertEqual(self.tries(), 3)
        # a good one waits a day before it is tried again
        c = self.creds(c, self.secrets('v3', CLAUDE_CODE_OAUTH_TOKEN='good'))
        self.assertEqual(self.tries(), 4)
        c.probe['at'] -= 3600
        self.creds(c)
        self.assertEqual(self.tries(), 4)
        c.probe['at'] -= self.k.TOKEN_CHECK_EVERY
        self.creds(c)
        self.assertEqual(self.tries(), 5)

    def test_a_token_try_that_cannot_tell_holds_nothing_and_comes_again_in_an_hour(self):
        self.fleet_claude()
        c = self.creds(None, self.secrets('v1', CLAUDE_CODE_OAUTH_TOKEN='slow'))
        self.assertEqual(c.state['claude'], ('ok', ''))     # what auth status said stands
        self.assertEqual(c.blocked(), {})
        self.creds(c)
        self.assertEqual(self.tries(), 1)
        c.probe['at'] -= 3600
        self.creds(c)
        self.assertEqual(self.tries(), 2)

    def test_a_login_without_the_fleet_token_is_not_tried(self):
        open(os.path.join(self.home, 'claude-login'), 'w').close()
        c = self.creds()
        self.assertEqual(c.state['claude'], ('ok', ''))
        self.assertIsNone(c.probe)

    def test_codex_login_does_not_hold_claude_bots(self):
        open(os.path.join(self.home, 'claude-login'), 'w').close()
        c = self.creds()
        self.assertNotIn('codex', c.state)      # no codex bot here: not checked, not reported
        self.assertEqual(c.blocked(), {})

    def test_board_secrets_are_written_private_and_used(self):
        c = self.creds()
        self.assertEqual(c.state['gh'][0], 'missing')
        c = self.creds(c, self.secrets('v1', GH_TOKEN='good'))
        self.assertEqual(stat.S_IMODE(os.stat(self.local).st_mode), 0o600)
        self.assertTrue(Path(self.local).read_text().startswith(self.k.FROM_BOARD))
        self.assertEqual(c.env()['GH_TOKEN'], 'good')
        self.assertEqual(c.state['gh'][0], 'ok')
        # rotated to a bad token on the board: the next report carries a new version
        c = self.creds(c, self.secrets('v2', GH_TOKEN='revoked'))
        self.assertEqual(c.state['gh'][0], 'expired')
        self.assertNotIn('revoked', str(c.report({})))   # the secret never goes into the report

    def test_fetched_only_when_the_version_changes(self):
        c = self.creds(None, self.secrets('v1', GH_TOKEN='good'))
        self.creds(c, 'v1'); self.creds(c, 'v1')
        self.assertEqual(self.fetches, 1)
        self.creds(c, self.secrets('v2', GH_TOKEN='better'))
        self.assertEqual(self.fetches, 2)

    def test_a_refused_fetch_keeps_ours_and_says_so(self):
        with open(self.local, 'w') as f:
            f.write('GH_TOKEN=good\n')
        c = self.creds(None, 'v1', token='not-a-keeper')
        self.assertEqual(c.state['gh'][0], 'ok')
        self.assertIn('stale', [r['state'] for r in c.report({})])
        self.assertEqual(Path(self.local).read_text(), 'GH_TOKEN=good\n')

    def test_no_board_secrets_leave_a_hand_kept_file_but_clear_ours(self):
        with open(self.local, 'w') as f:
            f.write('GH_TOKEN=good\n')
        c = self.creds(None, 'v1')                      # the board has none: ours stays
        self.assertEqual(Path(self.local).read_text(), 'GH_TOKEN=good\n')
        c = self.creds(c, self.secrets('v2', GH_TOKEN='good'))
        c = self.creds(c, self.secrets('v3'))           # removed on the board: removed here
        self.assertNotIn('GH_TOKEN', c.env())

    def test_an_unclear_check_holds_nothing(self):
        # an older claude without `auth status`, then one that prints something we cannot read
        for script in ('echo "error: unknown command auth" >&2; exit 2', 'echo "Loading..."'):
            fake(self.bin, 'claude', script)
            c = self.creds()
            self.assertEqual(c.state['claude'][0], 'unknown')
            self.assertEqual(c.blocked(), {})
            self.assertNotIn('claude claude', c.state['claude'][1])

    def test_gh_reports_the_failed_host_after_healthy_keyring_logins(self):
        healthy = (
            "github.com\n"
            "  Logged in to github.com account first (keyring)\n"
            "  - Active account: true\n"
            "  - Git operations protocol: https\n"
            "  - Token: gho_************************************\n"
            "  - Token scopes: gist, read:org, repo, workflow\n"
            "  Logged in to github.com account second (keyring)\n"
        )
        failed = (
            "git.corp.example.com\n"
            "  X Failed to log in to git.corp.example.com account jac (keyring)\n"
            "  - Active account: true\n"
            "  - The token in keyring is invalid.\n"
        )
        for stream in ('stdout', 'stderr'):
            for token in (None, 'good'):
                with self.subTest(stream=stream, token=token):
                    redirect = ' >&2' if stream == 'stderr' else ''
                    fake(self.bin, 'gh', f"cat{redirect} <<'STATUS'\n{healthy}{failed}STATUS\nexit 1")
                    c = self.k.Credentials('box')
                    env = c.env()
                    env.pop('GH_TOKEN', None)
                    if token:
                        env['GH_TOKEN'] = token
                    state, detail = c.check(self.k.CREDENTIALS[0], env)
                    self.assertEqual(state, 'expired')
                    self.assertIn('git.corp.example.com', detail)
                    self.assertIn('keyring is invalid', detail)
                    self.assertNotIn('account first', detail)
                    self.assertLessEqual(len(detail), 200)

    def test_fleet_env_sets_secrets_only_and_the_environment_wins(self):
        with open(self.local, 'w') as f:
            f.write('GH_TOKEN=good\nBOARD=http://other\nPATH=/nowhere\nnot_a_command\nOTHER_KEY=$(touch x)y\n')
        os.environ['OTHER_KEY'] = 'mine'
        try:
            env = self.creds().env()
        finally:
            os.environ.pop('OTHER_KEY')
        self.assertEqual(env['GH_TOKEN'], 'good')
        self.assertEqual(env['OTHER_KEY'], 'mine')
        self.assertEqual(env['PATH'], os.environ['PATH'])
        self.assertNotEqual(env.get('BOARD'), 'http://other')


class BotReadsFleetEnv(unittest.TestCase):
    def test_a_bad_line_does_not_stop_bin_bot(self):
        import subprocess
        with tempfile.TemporaryDirectory() as home:
            cfg = os.path.join(home, '.config', 'bot_board'); os.makedirs(cfg)
            with open(os.path.join(cfg, 'fleet.env'), 'w') as f:
                f.write(f'not_a_command_xyz\nGH_TOKEN=abc def\nX_TOKEN=$(touch {home}/ran)x\n')
            r = subprocess.run([str(ROOT / 'bin' / 'bot'), 'ps'], capture_output=True, text=True, timeout=60,
                               env={**os.environ, 'HOME': home, 'BOT_CONFIG': os.path.join(cfg, 'config')})
            self.assertNotIn('command not found', r.stderr)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(os.path.join(home, 'ran')))


class Page(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, 'board.db')
        db._local = threading.local()
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, 'conn'):
            db._local.conn.close()
        self.tmp.cleanup()

    def test_fleet_shows_credentials(self):
        tok = self.client.post('/api/agents', json={'handle': 'keeper-box', 'kind': 'keeper'}).json()['token']
        r = self.client.put('/api/fleet/hosts/box', headers={'Authorization': 'Bearer ' + tok}, json={
            'bots': [], 'credentials': [
                {'name': 'gh', 'kind': 'secret', 'state': 'ok'},
                {'name': 'claude', 'kind': 'login', 'state': 'missing', 'detail': 'not logged in', 'held': 2}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get('/api/fleet').json()['hosts'][0]['credentials'][1]['held'], 2)
        page = self.client.get('/fleet').text
        self.assertIn('claude missing on box', page)
        self.assertIn('not logged in. It is holding 2 bots.', page)
        self.assertIn('<span class="">gh</span>, <span class="prob">claude missing</span>', page)


    def test_a_rejected_fleet_token_says_how_to_replace_it(self):
        tok = self.client.post('/api/agents', json={'handle': 'keeper-box', 'kind': 'keeper'}).json()['token']
        self.client.put('/api/fleet/hosts/box', headers={'Authorization': 'Bearer ' + tok}, json={
            'bots': [], 'credentials': [{'name': 'claude', 'kind': 'login', 'state': 'rejected', 'held': 1,
                                         'detail': 'token rejected: API Error: 401 OAuth access token is invalid.'}]})
        page = self.client.get('/fleet').text
        self.assertIn('claude rejected on box', page)
        self.assertIn('Make a new one with <code>claude setup-token</code>', page)
        self.assertIn('claude rejected on box, 1 bot held', str(self.client.get('/api/status').json()['attention']))

    def test_the_fleet_claude_token_is_flagged_two_weeks_before_a_year(self):
        admin = {'Authorization': 'Bearer ' + self.client.post('/api/agents', json={'handle': 'op', 'kind': 'human'}).json()['token']}
        self.client.put('/api/fleet/secrets/CLAUDE_CODE_OAUTH_TOKEN', headers=admin, json={'value': 'sk-ant-oat01-x'}).raise_for_status()
        self.assertNotIn("claude token", self.client.get('/fleet', headers=admin).text.split('Settings: shared secrets')[0])
        self.assertNotIn("claude token", str(self.client.get('/api/status').json()['attention']))
        with db.conn() as c:
            c.execute("UPDATE fleet_secrets SET set_at=? WHERE name='CLAUDE_CODE_OAUTH_TOKEN'",
                      ((datetime.now(timezone.utc) - timedelta(days=355)).isoformat(),))
        page = self.client.get('/fleet', headers=admin).text
        self.assertIn("The fleet's claude token expires in 10 days", page)
        self.assertIn("The fleet's claude token expires in 10 days", str(self.client.get('/api/status').json()['attention']))

    def test_the_runtime_a_box_reports_shows_on_its_machine_row(self):
        # #479: the keeper's runtime check reaches /fleet; a problem is marked, a keeper from before shows nothing
        keeper = {'Authorization': 'Bearer ' + self.client.post('/api/agents', json={'handle': 'keeper-box', 'kind': 'keeper'}).json()['token']}
        admin = {'Authorization': 'Bearer ' + self.client.post('/api/agents', json={'handle': 'op', 'kind': 'human'}).json()['token']}
        self.client.put('/api/fleet/hosts/box', headers=keeper, json={'bots': [], 'runtime': {
            'setting': 'docker', 'state': 'ok', 'detail': '29.7.2'}}).raise_for_status()
        self.assertIn('runtime docker 29.7.2</div>', self.client.get('/fleet', headers=admin).text)
        self.client.put('/api/fleet/hosts/box', headers=keeper, json={'bots': [], 'runtime': {
            'setting': 'colima', 'state': 'missing', 'detail': "no colima on this box's PATH"}}).raise_for_status()
        page = self.client.get('/fleet', headers=admin).text
        self.assertIn('class="by prob" title="no colima on this box&#x27;s PATH">runtime colima missing</div>', page)
        self.client.put('/api/fleet/hosts/box', headers=keeper, json={'bots': []}).raise_for_status()
        self.assertNotIn('runtime ', self.client.get('/fleet', headers=admin).text.split('id="box-box"')[1].split('</tr>')[0])

    def test_the_board_tells_keepers_and_healthz_the_commit_it_runs(self):
        # #240: keepers update to the board's release commit; "" from source, origin's tip for them
        keeper = {'Authorization': 'Bearer ' + self.client.post('/api/agents', json={'handle': 'keeper-box', 'kind': 'keeper'}).json()['token']}
        self.addCleanup(setattr, main, 'COMMIT', main.COMMIT)
        for commit in ('', 'a' * 40):
            main.COMMIT = commit
            got = self.client.put('/api/fleet/hosts/box', headers=keeper, json={'bots': []}).json()
            self.assertEqual((got['release'], self.client.get('/healthz').json()['commit']), (commit, commit))

    def test_secrets_are_set_by_humans_and_read_by_keepers_only(self):
        def agent(handle, kind):
            return {'Authorization': 'Bearer ' + self.client.post('/api/agents', json={'handle': handle, 'kind': kind}).json()['token']}
        admin, human, bot, keeper, idle = (agent('op', 'human'), agent('other', 'human'), agent('ada', 'claude-code'),
                                            agent('keeper-box', 'keeper'), agent('keeper-x', 'keeper'))
        report = lambda: self.client.put('/api/fleet/hosts/box', headers=keeper, json={'bots': []}).json()['secrets']
        v0 = report()
        self.assertEqual(self.client.put('/api/fleet/secrets/GH_TOKEN', headers=bot, json={'value': 'x'}).status_code, 403)
        self.assertEqual(self.client.put('/api/fleet/secrets/PATH', headers=admin, json={'value': 'x'}).status_code, 422)
        self.assertEqual(self.client.put('/api/fleet/secrets/GH_TOKEN', headers=admin, json={'value': 'a\nb'}).status_code, 422)
        r = self.client.put('/api/fleet/secrets/GH_TOKEN', headers=admin, json={'value': 'ghp_secret1234'})
        self.assertEqual(r.status_code, 200, r.text)
        listed = self.client.get('/api/fleet/secrets', headers=admin).json()['secrets']
        self.assertEqual([(x['name'], x['ends'], x['set_by']) for x in listed], [('GH_TOKEN', '1234', 'op')])
        self.assertNotIn('ghp_secret1234', self.client.get('/api/fleet/secrets', headers=admin).text)
        self.assertNotEqual(report(), v0)
        # the values: the keeper of a box, and nobody else
        self.assertEqual(self.client.get('/api/fleet/secrets/values', headers=keeper).json()['secrets'],
                         {'GH_TOKEN': 'ghp_secret1234'})
        for who in (admin, bot, idle):
            self.assertEqual(self.client.get('/api/fleet/secrets/values', headers=who).status_code, 403)
        self.assertNotIn('ghp_secret1234', self.client.get('/api/fleet').text + self.client.get('/fleet').text)
        self.assertEqual(self.client.delete('/api/fleet/secrets/GH_TOKEN', headers=admin).status_code, 200)
        self.assertEqual(report(), v0)


class BotReloadsFleetEnv(unittest.TestCase):
    def test_rotated_and_removed_secrets_reach_the_next_run(self):
        import subprocess
        src = (ROOT / 'bin' / 'bot').read_text()
        fn = src[src.index('_OWN_SECRETS='):src.index('\nload_fleet_env\n')]
        with tempfile.TemporaryDirectory() as home:
            env_file = os.path.join(home, '.config', 'bot_board', 'fleet.env'); os.makedirs(os.path.dirname(env_file))
            script = fn + f'''
w() {{ printf '%s\\n' "$1" > {env_file}; }}
w GH_TOKEN=one; load_fleet_env; echo "gh=$GH_TOKEN own=$MY_KEY"
w GH_TOKEN=two; load_fleet_env; echo "gh=$GH_TOKEN"
w MY_KEY=theirs; load_fleet_env; echo "gh=${{GH_TOKEN:-gone}} own=$MY_KEY"
'''
            r = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True,
                               env={'PATH': os.environ['PATH'], 'HOME': home, 'MY_KEY': 'mine'})
            self.assertEqual(r.stdout.split('\n')[:3], ['gh=one own=mine', 'gh=two', 'gh=gone own=mine'], r.stderr)

if __name__ == '__main__':
    unittest.main()


class ServiceFileTests(unittest.TestCase):
    """#153: settings live in the config file; the service file must not freeze them."""

    def test_service_carries_path_and_config_location_only(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as home:
            k = load_keeper(home)
            unit = os.path.join(home, 'bot-board-keeper.service')
            env = {'PATH': '/harnesses', 'BOT_CONFIG': os.path.join(home, 'config'),
                   'BOARD': 'http://b:8080', 'KEEPER_OPERATORS': 'you', 'KEEPER_HOST': 'box',
                   'BOT_INVITE': 'secret', 'KEEPER_HANDOVER': 'h:dir'}
            with mock.patch.object(k, 'UNIT', unit), mock.patch.object(k.sys, 'platform', 'linux'), \
                    mock.patch.object(k, 'out', return_value='Linger=yes'), \
                    mock.patch.object(k.subprocess, 'run'), mock.patch.dict(os.environ, env):
                k.service(True)
            text = Path(unit).read_text()
        self.assertIn('Environment="PATH=/harnesses"', text)
        self.assertIn('Environment="BOT_CONFIG=', text)
        for key in ('BOARD', 'KEEPER_OPERATORS', 'KEEPER_HOST', 'BOT_INVITE', 'KEEPER_HANDOVER'):
            self.assertNotIn(key + '=', text)
