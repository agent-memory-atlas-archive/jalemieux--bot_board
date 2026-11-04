"""#478: with BOT_RUNTIME set, the keeper is the only process besides bin/bot that talks to the
container runtime. It pulls each harness image (a harness update is a new :latest), runs the
login checks inside the image, and reports the digest each image has. Run with
python -m unittest discover -s tests."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_keeper_creds import fake, load_keeper      # noqa: E402
from fastapi.testclient import TestClient      # noqa: E402
from app import db, main      # noqa: E402

IMAGE = "minipc-2.taild87368.ts.net:3000/jac/bot_board-harness-"


class KeeperImages(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ); env.start(); self.addCleanup(env.stop)
        for k in ('GH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'BOT_RUNTIME', 'BOT_HARNESS_REGISTRY'):
            os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.bin = os.path.join(self.home, 'bin'); os.makedirs(self.bin)
        os.makedirs(os.path.join(self.home, '.config', 'bot_board'))
        self.log = os.path.join(self.home, 'docker-calls')
        # a docker CLI: pull makes the image exist; run executes what follows the image, as the image would
        fake(self.bin, 'docker', f'echo "$*" >> {self.log}\n'
             f'case "$1 $2" in\n'
             f' "pull -q") [ -e {self.home}/registry-down ] && {{ echo "connection refused" >&2; exit 1; }}\n'
             f'   touch "{self.home}/have-$(basename "$3")"; exit 0 ;;\n'
             f' "image inspect") img=$(eval echo "\\${{$#}}"); [ -e "{self.home}/have-$(basename "$img")" ] || exit 1\n'
             f'   [ "$3" = --format ] && echo "$img@sha256:0123456789abcdef0123"; exit 0 ;;\n'
             f'esac\n'
             f'[ "$1" = run ] || exit 2\n'
             f'while [ $# -gt 0 ]; do case "$1" in *bot_board-harness-*) shift; break ;; esac; shift; done\n'
             f'exec "$@"')
        # claude inside the image: logged in when the fleet's token reached it
        fake(self.bin, 'claude', 'if [ "$1" = auth ]; then [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ] && '
                                 'echo \'{"loggedIn": true}\' || echo \'{"loggedIn": false}\'; exit 0; fi\n'
                                 'echo \'{"is_error": false, "result": "OK."}\'')
        os.environ['PATH'] = self.bin + os.pathsep + os.environ['PATH']
        self.k = load_keeper(self.home)

    def runtime(self, value):
        with open(os.path.join(self.home, '.config', 'bot_board', 'config'), 'w') as fh:     # the keeper's CFG
            fh.write(f'BOT_RUNTIME={value}\n')

    def calls(self):
        try:
            with open(self.log) as fh:
                return fh.read().splitlines()
        except FileNotFoundError:
            return []

    def pulled(self, images, harnesses=('claude',)):
        images.tick(set(harnesses), force=True)
        for _ in range(100):
            if all(r['state'] != 'pulling' for r in images.report()):
                return images.report()
            time.sleep(0.05)
        self.fail('the pull did not finish')

    def test_the_host_path_does_not_touch_the_runtime(self):
        images = self.k.Images()
        images.tick({'claude'}, force=True)
        self.assertEqual(images.report(), [])
        c = self.k.Credentials('box'); c.refresh({'claude'}, force=True)
        self.assertEqual(c.state['claude'][0], 'missing')      # host claude, no token: as before
        self.assertEqual(self.calls(), [])

    def test_the_image_is_pulled_and_the_login_checked_inside_it(self):
        self.runtime('docker')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN'] = 'sekrit-value'
        c = self.k.Credentials('box')
        self.assertEqual(c.check(self.k.CREDENTIALS[1], c.env())[0], 'missing', 'no image yet holds claude bots')
        rows = self.pulled(self.k.Images())
        self.assertEqual(rows, [{'harness': 'claude', 'image': IMAGE + 'claude:latest', 'state': 'ok',
                                 'digest': 'sha256:0123456789abcdef0123', 'detail': '', 'at': rows[0]['at']}])
        self.assertEqual(c.check(self.k.CREDENTIALS[1], c.env()), ('ok', ''))
        run = [line for line in self.calls() if line.startswith('run ')]
        self.assertEqual(run, [f'run --rm -e CLAUDE_CODE_OAUTH_TOKEN {IMAGE}claude:latest claude auth status'])
        self.assertNotIn('sekrit-value', '\n'.join(self.calls()), 'secrets go in by name, never as a value')

    def test_the_keepers_own_environment_stays_out(self):
        # what the keeper's service has in its environment (its own token, say) is not a bot's secret
        os.environ.update(BOARD_TOKEN='keeper-own', FORGEJO_TOKEN='forge-own', CLAUDE_CODE_OAUTH_TOKEN='fleet')
        with open(self.k.FLEET_ENV, 'w') as fh:
            fh.write('GH_TOKEN=gh\n')
        c = self.k.Credentials('box')
        argv = self.k.in_image('docker', 'claude', ['claude', 'auth', 'status'], c.env())
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == '-e'], ['CLAUDE_CODE_OAUTH_TOKEN', 'GH_TOKEN'])

    def test_the_token_is_tried_in_the_image(self):
        self.runtime('docker')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN'] = 'good'
        self.pulled(self.k.Images())
        c = self.k.Credentials('box'); c.refresh({'claude'}, force=True)
        self.assertEqual(c.state['claude'], ('ok', ''))
        self.assertTrue(any(line.startswith('run --rm -e CLAUDE_CODE_OAUTH_TOKEN') and ' claude -p ' in line
                            for line in self.calls()), self.calls())

    def test_a_failed_pull_keeps_the_image_here(self):
        self.runtime('docker')
        images = self.k.Images()
        self.pulled(images)
        open(os.path.join(self.home, 'registry-down'), 'w').close()
        row = self.pulled(images)[0]
        self.assertEqual((row['state'], row['digest']), ('stale', 'sha256:0123456789abcdef0123'))
        self.assertIn('connection refused', row['detail'])
        # a box that never had the image says so
        first = self.pulled(self.k.Images(), ('codex',))[0]
        self.assertEqual((first['harness'], first['state'], first['digest']), ('codex', 'missing', ''))

    def test_codex_reads_its_login_read_only(self):
        self.runtime('docker')
        os.makedirs(os.path.join(self.home, '.codex'))
        open(os.path.join(self.home, '.codex', 'auth.json'), 'w').close()
        with open(self.k.FLEET_ENV, 'w') as fh:
            fh.write('GH_TOKEN=x\n')
        env = {'PATH': '/bin', 'GH_TOKEN': 'x', 'BOARD_TOKEN': 'the keeper\'s own'}
        argv = self.k.in_image('docker', 'codex', ['codex', 'login', 'status'], env)
        self.assertEqual(argv, ['docker', 'run', '--rm', '-e', 'GH_TOKEN', '-v',
                                f'{self.home}/.codex/auth.json:/home/bot/.codex/auth.json:ro',
                                IMAGE + 'codex:latest', 'codex', 'login', 'status'])

    def test_the_registry_is_a_box_setting(self):
        os.environ['BOT_HARNESS_REGISTRY'] = 'reg.example:5000'
        self.assertEqual(self.k.harness_image('opencode'), 'reg.example:5000/jac/bot_board-harness-opencode:latest')



class ImagesOnFleet(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        for name, value in (('DB_PATH', os.path.join(tmp.name, 'board.db')), ('_local', threading.local())):
            p = patch.object(db, name, value); p.start(); self.addCleanup(p.stop)
        self.client = TestClient(main.app); self.client.__enter__()
        self.addCleanup(lambda: hasattr(db._local, 'conn') and db._local.conn.close())
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_the_report_carries_the_images_and_fleet_shows_them(self):
        tok = self.client.post('/api/agents', json={'handle': 'keeper-box', 'kind': 'keeper'}).json()['token']
        r = self.client.put('/api/fleet/hosts/box', headers={'Authorization': 'Bearer ' + tok}, json={
            'bots': [], 'harness_images': [
                {'harness': 'claude', 'image': IMAGE + 'claude:latest', 'state': 'ok',
                 'digest': 'sha256:0123456789abcdef0123', 'detail': '', 'at': '2026-11-04T20:00:00+00:00'},
                {'harness': 'codex', 'image': IMAGE + 'codex:latest', 'state': 'missing', 'digest': '',
                 'detail': 'pull failed: connection refused', 'at': '2026-11-04T20:00:00+00:00'}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get('/api/fleet').json()['hosts'][0]['harness_images'][0]['digest'],
                         'sha256:0123456789abcdef0123')
        page = self.client.get('/fleet').text
        self.assertIn('<div class="by prob">images ', page)
        self.assertIn('claude 0123456789ab</span>', page)
        self.assertIn('pull failed: connection refused">codex missing</span>', page)

if __name__ == '__main__':
    unittest.main()
