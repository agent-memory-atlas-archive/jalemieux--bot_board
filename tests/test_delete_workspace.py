"""#473: a human deletes an archived channel or workspace, and everything in it, after a warning
that says what goes. Archived first, the slug typed back, and a workspace with bots refused.
Run with python -m unittest discover -s tests."""
import base64
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_workspaces import Base       # noqa: E402
from app import db, storage            # noqa: E402

PDF = base64.b64encode(b"%PDF-1.4 only here").decode()


class DeleteWorkspace(Base):
    def setUp(self):
        super().setUp()
        self.human = self.register('jac', 'human')
        self.call('POST', '/api/workspaces', {'slug': 'game', 'name': 'Space Game', 'purpose': 'A game'},
                  auth=self.human, expected=201)
        self.call('POST', '/api/workspaces/game/channels', {'slug': 'art', 'name': 'Art'}, auth=self.human, expected=201)
        root = self.post(self.human, 'a thread with a file', channel='game.art')['message']['id']
        self.post(self.human, 'a reply', channel='game.art', reply_to=root)
        self.call('POST', '/api/messages', {'channel': 'game.art', 'body': 'the spec', 'reply_to': root,
                                            'file_attachment': {'name': 'spec.pdf', 'content': PDF}},
                  auth=self.human, expected=201)
        self.key = db.conn().execute('SELECT storage_key FROM file_attachments').fetchone()[0]
        self.root = root
        self.call('PUT', '/api/workspaces/game/knowledge/INDEX.md', {'body': 'notes', 'base_revision': 0, 'reason': 'x'},
                  auth=self.human)

    def test_a_channel_is_archived_first_confirmed_by_slug_then_gone_with_its_files(self):
        gone = self.call('GET', '/api/workspaces/game/channels/art/deletion', auth=self.human)['deletion']
        self.assertEqual((gone['threads'], gone['messages'], gone['attachments']), (1, 3, 1))
        bot = self.register('ada', 'claude-code', workspace='game')
        self.call('GET', '/api/workspaces/game/channels/art/deletion', auth=bot, expected=403)
        self.call('DELETE', '/api/workspaces/game/channels/art', {'confirm': 'game.art'}, auth=self.human, expected=409)
        self.call('PATCH', '/api/workspaces/game/channels/art', {'archived': True}, auth=self.human)
        self.call('DELETE', '/api/workspaces/game/channels/art', {'confirm': 'art'}, auth=self.human, expected=422)
        self.call('DELETE', '/api/workspaces/game/channels/art', {'confirm': 'game.art'}, auth=bot, expected=403)
        self.call('DELETE', '/api/workspaces/game/channels/game', {'confirm': 'game'}, auth=self.human, expected=422)
        out = self.call('DELETE', '/api/workspaces/game/channels/art', {'confirm': 'game.art'}, auth=self.human)
        self.assertEqual(out['deleted']['messages'], 3)
        self.assertIsNone(db.get_board('game.art'))
        self.assertIsNone(db.get_message(self.root))
        self.assertFalse(os.path.exists(storage.path(self.key)), 'the file goes when nothing else uses it')
        self.call('GET', '/api/workspaces/game', auth=self.human)     # the workspace is untouched

    def test_a_workspace_with_bots_is_refused_and_without_them_goes_whole(self):
        self.register('ada', 'claude-code', workspace='game')
        wid = db.conn().execute("SELECT id FROM workspaces WHERE slug='game'").fetchone()[0]
        gone = self.call('GET', '/api/workspaces/game/deletion', auth=self.human)['deletion']
        self.assertEqual(gone['bots'], ['ada'])
        self.assertEqual(gone['documents'], 1)
        self.assertEqual(sorted(gone['channels']), ['game', 'game.art'])
        self.call('DELETE', '/api/workspaces/game', {'confirm': 'game'}, auth=self.human, expected=409)   # not archived
        self.call('PATCH', '/api/workspaces/game', {'archived': True, 'archive_confirm': True}, auth=self.human)
        refused = self.call('DELETE', '/api/workspaces/game', {'confirm': 'game'}, auth=self.human, expected=409)
        self.assertIn('@ada', refused['error'])
        self.call('PUT', '/api/workspaces/admin/bots', {'bots': ['ada']}, auth=self.human)     # moved away
        self.call('DELETE', '/api/workspaces/game', {'confirm': 'gam'}, auth=self.human, expected=422)
        self.call('DELETE', '/api/workspaces/game', {'confirm': 'game'}, auth=self.human)
        self.call('GET', '/api/workspaces/game', auth=self.human, expected=404)
        self.assertIsNone(db.get_board('game'))
        self.assertIsNone(db.get_board('game.art'))
        self.assertEqual(db.conn().execute('SELECT COUNT(*) FROM knowledge_docs WHERE workspace_id=?', (wid,)).fetchone()[0], 0)
        self.assertFalse(os.path.exists(storage.path(self.key)))
        # the slug is free again
        self.call('POST', '/api/workspaces', {'slug': 'game', 'name': 'Again', 'purpose': 'x'}, auth=self.human, expected=201)

    def test_the_page_warns_with_counts_and_deletes_only_on_the_typed_slug(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Install Playwright and Chromium for page checks")
        import json
        self.call('PATCH', '/api/workspaces/game/channels/art', {'archived': True}, auth=self.human)
        token = self.human['Authorization'].split()[1]
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                errors, asked, answers = [], [], ['art', 'game.art']
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('dialog', lambda d: (asked.append(d.message), d.accept(answers.pop(0))))

                def serve(route):
                    q = route.request
                    r = self.client.request(q.method, q.url.replace('http://board.test', ''), content=q.post_data, headers=q.headers)
                    route.fulfill(status=r.status_code, body=r.content, content_type=r.headers.get('content-type', 'text/plain'))
                page.route('**/*', serve)
                page.add_init_script("localStorage.setItem('bb.me', " + json.dumps(json.dumps({'handle': 'jac', 'token': token})) + ")")
                page.goto('http://board.test/workspaces/game')
                row = page.locator('.ch', has_text='#game.art')
                row.locator('summary').click()
                row.locator('button[data-del]').click()
                row.locator('.msg-err').filter(has_text='does not match').wait_for()
                self.assertIsNotNone(db.get_board('game.art'), 'a wrong slug deletes nothing')
                self.assertIn('1 thread, 3 messages, 1 attachment', asked[0])
                self.assertIn('cannot be undone', asked[0])
                with page.expect_navigation():
                    row.locator('button[data-del]').click()
                self.assertIsNone(db.get_board('game.art'))
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_a_bot_bound_only_to_the_channel_blocks_it_and_others_lose_the_binding(self):
        self.call('PATCH', '/api/workspaces/game/channels/art', {'archived': True}, auth=self.human)
        c = db.conn()
        for handle, chans in (('artist', ['game.art']), ('writer', ['game.art', 'game'])):
            c.execute("INSERT INTO fleet_bots (handle, host, harness, run, set_by, set_at, channels) VALUES (?,?,?,?,?,?,?)",
                      (handle, 'box', 'claude', 1, 'jac', db.now(), json.dumps(chans)))
        c.commit()
        self.assertEqual(self.call('GET', '/api/workspaces/game/channels/art/deletion', auth=self.human)['deletion']['bound_only'], ['artist'])
        refused = self.call('DELETE', '/api/workspaces/game/channels/art', {'confirm': 'game.art'}, auth=self.human, expected=409)
        self.assertIn('@artist', refused['error'])
        c.execute("DELETE FROM fleet_bots WHERE handle='artist'"); c.commit()
        self.call('DELETE', '/api/workspaces/game/channels/art', {'confirm': 'game.art'}, auth=self.human)
        self.assertEqual(db.bot_channels('writer'), ['game'])

    def test_the_admin_workspace_cannot_be_deleted(self):
        self.call('DELETE', '/api/workspaces/admin', {'confirm': 'admin'}, auth=self.human, expected=422)


if __name__ == '__main__':
    unittest.main()
