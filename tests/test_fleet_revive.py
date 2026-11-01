"""Add a bot suggests reviving a stopped bot instead of starting a new one (#168).
Run with python -m unittest discover -s tests."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import db, main


class Revive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, 'board.db')
        db._local = threading.local()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.human = self.register('op', 'human')
        self.k1, self.k2 = self.register('keeper-one', 'keeper'), self.register('keeper-two', 'keeper')
        self.bot = self.register('tarzan', 'claude-code')
        r = self.client.post('/api/messages', headers=self.human, json={'channel': 'proj', 'body': 'job'})
        self.client.post(f"/api/threads/{r.json()['message']['id']}/worker", headers=self.bot).raise_for_status()
        self.report('one', self.k1, [self.b('tarzan', 'stopped'), self.b('jane', 'stopped'),
                                     self.b('cheeta', 'stopped', harness='codex'), self.b('up', 'waiting')])
        self.report('two', self.k2, [])

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, 'conn'):
            db._local.conn.close()
        self.tmp.cleanup()

    def register(self, handle, kind):
        r = self.client.post('/api/agents', json={'handle': handle, 'kind': kind})
        self.assertEqual(r.status_code, 201, r.text)
        return {'Authorization': 'Bearer ' + r.json()['token']}

    @staticmethod
    def b(handle, status, harness='claude'):
        return {'handle': handle, 'harness': harness, 'status': status, 'serves': '/pool', 'pool': True}

    def report(self, host, auth, bots):
        r = self.client.put(f'/api/fleet/hosts/{host}', headers=auth, json={'bots': bots})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['want']

    def candidates(self):
        page = self.client.get('/fleet').text
        return json.loads(re.search(r'<script type="application/json" id="revive-data">(.*?)</script>', page).group(1))

    def test_stopped_bots_are_offered_open_threads_first_never_retired(self):
        got = self.candidates()
        self.assertEqual([c['handle'] for c in got], ['tarzan', 'cheeta', 'jane'])
        self.assertEqual(got[0]['open'], 1)
        self.assertEqual(got[0]['host'], 'one')
        r = self.client.put('/api/fleet/bots/jane', headers=self.human,
                            json={'host': 'one', 'run': False, 'threads': 'retire'})
        self.assertEqual(r.status_code, 200, r.text)
        self.report('one', self.k1, [self.b('tarzan', 'stopped'), self.b('jane', 'stopped')])
        self.assertNotIn('jane', [c['handle'] for c in self.candidates()])

    def test_move_here_and_revive_starts_it_on_the_new_box(self):
        r = self.client.post('/api/fleet/bots/tarzan/move', headers=self.human, json={'to': 'two', 'start': True})
        self.assertEqual(r.status_code, 200, r.text)
        want = {w['handle']: w for w in self.report('one', self.k1, [self.b('tarzan', 'stopped')])}
        self.assertEqual(want['tarzan']['move_to'], 'two')
        self.assertTrue(want['tarzan']['run'])     # the new box starts it once it has unpacked it

    def test_a_plain_move_of_a_stopped_bot_keeps_it_stopped(self):
        r = self.client.post('/api/fleet/bots/tarzan/move', headers=self.human, json={'to': 'two'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.report('one', self.k1, [self.b('tarzan', 'stopped')])[0]['run'])

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_the_form_filters_by_machine_harness_and_project(self):
        page = self.client.get('/fleet').text
        start = page.index('var REVIVE =')
        js = page[start:page.index('// The fleet\'s secrets', start)]
        script = r"""
const assert = require('node:assert/strict');
const els = {host: {value: 'one'}, harness: {value: 'claude'}, workspace: {value: ''}};
const form = {elements: els}, box = {hidden: true, innerHTML: ''};
const document = {querySelector: () => form, addEventListener() {},
  getElementById: id => id === 'revive' ? box : {textContent: DATA}};
let ME = null; function ask() {}
""" + f"const DATA = {json.dumps(json.dumps(self.candidates()))};\n" + js + r"""
assert.equal(box.hidden, false);
assert.match(box.innerHTML, /Revive a stopped bot instead\?/);
assert.match(box.innerHTML, /@tarzan <small><b>1 open thread<\/b> · on one<\/small>.*>Revive</);
assert.doesNotMatch(box.innerHTML, /cheeta/);           // another harness
els.host.value = 'two'; reviveList();
assert.match(box.innerHTML, /data-to="two">Move here and revive/);
els.workspace.value = 'other'; reviveList();
assert.equal(box.hidden, true);
"""
        r = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
