"""#232: the header's chip counts the bots the fleet reports running, not every recent token."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import db, fleet, main, web


class RunningChip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, 'board.db')
        db._local = threading.local()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.register('jac', 'human')
        self.keeper = self.register('keeper-box', 'keeper')
        self.register('routines', 'cron')
        for h in ('ann', 'bob', 'cal', 'dot', 'eve'):
            self.register(h, 'claude-code')

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, 'conn'):
            db._local.conn.close()
        self.tmp.cleanup()

    def register(self, handle, kind):
        r = self.client.post('/api/agents', json={'handle': handle, 'kind': kind})
        self.assertEqual(r.status_code, 201, r.text)
        return {'Authorization': 'Bearer ' + r.json()['token']}

    def seen(self, handles, at):
        marks = ','.join('?' * len(handles))
        db.conn().execute(f'UPDATE agents SET last_seen_at=? WHERE handle IN ({marks})', (at, *handles))
        db.conn().commit()

    def report(self):
        r = self.client.put('/api/fleet/hosts/box', headers=self.keeper, json={'bots': [
            {'handle': 'ann', 'harness': 'claude', 'status': 'waiting'},
            {'handle': 'bob', 'harness': 'claude', 'status': 'running 7756'},
            {'handle': 'cal', 'harness': 'claude', 'status': 'stopped'},
            {'handle': 'dot', 'harness': 'claude', 'status': 'waiting', 'health': 'logged out since 00:00'},
        ]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_counts_running_bots_not_keepers_runner_humans_or_stray_tokens(self):
        self.report()
        # everyone made a request just now: cal's token was used by hand, though its launcher is stopped
        self.seen(['jac', 'keeper-box', 'routines', 'ann', 'bob', 'cal', 'dot'], db.now())
        self.assertEqual(db.stats()['active'], 7)       # presence itself is unchanged
        self.assertEqual(fleet.running(), {'bots': 2, 'working': 1, 'keepers': 1, 'runners': 1, 'humans': 1})

    def test_a_bot_no_keeper_reports_counts_by_presence(self):
        self.report()
        self.seen(['eve'], db.now())
        self.assertEqual(fleet.running()['bots'], 3)
        self.seen(['eve'], '2026-01-01T00:00:00+00:00')
        self.assertEqual(fleet.running()['bots'], 2)

    def test_header_shows_running_with_breakdown(self):
        self.report()
        self.seen(['jac', 'keeper-box', 'routines'], db.now())
        page = self.client.get('/').text
        self.assertIn('<a href="/fleet" class="tab"', page)       # the count sits in the Fleet tab (thread 9918)
        self.assertIn('<span id="nag">2 running</span>', page)
        self.assertIn('title="2 bots running (1 working) · 1 keeper · 1 runner · 1 human"', page)
        self.assertNotIn(' active</a>', page)
        # the presence refresh leaves it alone: "active agents" is another number (#456, review)
        self.assertNotIn("$('#nag').textContent", web.APP_JS)
        self.assertEqual(web.running_chip(None), '')

    def test_the_top_bar_has_tabs_not_a_manage_menu(self):
        # thread 9918: Board, Fleet, Workspaces and Routines as tabs; the current page is marked
        for path, current in (('/', '/'), ('/fleet', '/fleet'), ('/routines', '/routines'), ('/workspaces', '/workspaces')):
            page = self.client.get(path).text
            self.assertNotIn('id="manage"', page, path)
            self.assertNotIn('id="navboard"', page, path)
            for href in ('/', '/fleet', '/workspaces', '/routines'):
                cur = ' aria-current="page"' if href == current else ''
                self.assertRegex(page, f'<a href="{href}" class="tab" title="[^"]*"{cur}>', (path, href))


if __name__ == '__main__':
    unittest.main()
