"""Routines on the board: stored rows, changed only through /api/routines, by the right people.
Run with python -m unittest discover -s tests."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import db, main, routines

CHECK = '#!/usr/bin/env bash\necho \'{"work": false}\'\n'


class Routines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, 'board.db')
        db._local = threading.local()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.human = self.register('jac', 'human')
        self.stranger = self.register('visitor', 'human')
        self.bot = self.register('ada', 'claude-code')
        self.runner = self.register(routines.cli.ME, 'cron')
        self.assertEqual(self.call('GET', '/api/workspaces/admin')['workspace']['slug'], 'admin')   # comes with the first admin (#103)
        for r in self.call('GET', '/api/routines')['routines']:   # the shipped routines (docs/routines) are tests/test_routines_seed.py's subject
            self.call('DELETE', f"/api/routines/{r['id']}")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, 'conn'):
            db._local.conn.close()
        self.tmp.cleanup()

    def register(self, handle, kind):
        r = self.client.post('/api/agents', json={'handle': handle, 'kind': kind})
        self.assertEqual(r.status_code, 201, r.text)
        return {'Authorization': 'Bearer ' + r.json()['token']}

    def call(self, method, path, data=None, auth=None, expected=200):
        r = self.client.request(method, path, json=data, headers=auth or self.human)
        self.assertEqual(r.status_code, expected, r.text)
        return r.json()

    def post(self, body, reply_to=None, auth=None, channel='admin', meta=None):
        return self.call('POST', '/api/messages', {'channel': channel, 'body': body, 'reply_to': reply_to,
                                                   'meta': meta or {}}, auth=auth, expected=201)['message']

    def add(self, auth=None, expected=201, **kw):
        data = {'name': 'triage', 'channel': 'bot_board', 'check': CHECK, 'prompt': 'Triage these.\r\n',
                'every': 60, 'about': 'triage new issues', **kw}
        return self.call('POST', '/api/routines', data, auth=auth, expected=expected)

    def make_admin(self, handle='ada'):
        """As a human does with Save bots on the admin workspace, without a keeper to report the bot."""
        c = db.conn()
        c.execute("UPDATE agents SET workspace_id=(SELECT id FROM workspaces WHERE slug='admin') WHERE handle=?", (handle,))
        c.commit()

    def test_admin_discovery_is_only_given_to_authorized_agents(self):
        for headers in ({}, self.bot):
            public = self.client.get('/api', headers=headers).json()
            self.assertNotIn('administration', public)
            self.assertFalse(any('/api/routines' in key or '/api/fleet' in key for key in public['endpoints']))
            for path in ('/api/admin', '/api/admin/openapi.json', '/admin/agents.md'):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 403)
        schema = self.client.get('/api/openapi.json').json()
        self.assertNotIn('/api/routines', schema['paths'])
        self.assertNotIn('RoutineIn', schema['components']['schemas'])
        self.assertIn('/api/messages', schema['paths'])
        self.assertIn('/api/knowledge/{path}', schema['paths'])
        self.assertNotIn('/api/workspaces/{slug}/knowledge/{path}', schema['paths'])
        self.assertEqual(self.client.get('/api/docs').status_code, 200)
        self.assertNotIn('POST /api/routines', self.client.get('/agents.md').text)
        self.make_admin()
        for headers in (self.bot, self.human):
            self.assertIn('administration', self.client.get('/api', headers=headers).json())
            self.assertIn('POST /api/routines', self.client.get('/api/admin', headers=headers).json()['endpoints'])
            self.assertIn('/api/routines', self.client.get('/api/admin/openapi.json', headers=headers).json()['paths'])
            self.assertIn('POST /api/routines', self.client.get('/admin/agents.md', headers=headers).text)
        db.conn().execute('UPDATE agents SET workspace_id=NULL'); db.conn().commit()
        self.assertEqual(self.client.get('/admin/agents.md', headers=self.bot).status_code, 403)

    def test_the_admin_workspace_comes_seeded_and_asks_for_its_bot(self):
        # #103: created with the first admin, its index written, and flagged until it has a bot
        p = self.call('GET', '/api/workspaces/admin')['workspace']
        self.assertEqual(p['channel'], 'admin')
        index = self.call('GET', '/api/workspaces/admin/knowledge/INDEX.md')['document']
        self.assertEqual(index['revision'], 1)
        self.assertIn('Seed requests', index['body'])
        self.assertTrue(any('workspace admin has no bot' in a['text'] for a in self.call('GET', '/api/status')['attention']))
        self.assertFalse(routines.ensure_admin_project())     # an existing one is left as it is
        self.call('PATCH', '/api/workspaces/admin', {'name': 'Ops'})
        self.assertEqual(self.call('GET', '/api/workspaces/admin')['workspace']['name'], 'Ops')

    def drop_admin(self, c):
        c.execute("DELETE FROM knowledge_docs WHERE workspace_id IN (SELECT id FROM workspaces WHERE slug='admin')")
        c.execute("DELETE FROM workspace_audit WHERE workspace_id IN (SELECT id FROM workspaces WHERE slug='admin')")
        c.execute("DELETE FROM workspaces WHERE slug='admin'")
        c.execute("UPDATE boards SET workspace_id=NULL WHERE slug='admin'"); c.commit()

    def test_a_board_with_an_admin_but_no_admin_workspace_gets_one_at_startup(self):
        c = db.conn()
        self.drop_admin(c)
        self.drop_admin(c)
        main._startup()
        p = self.call('GET', '/api/workspaces/admin')['workspace']
        self.assertEqual(p['channel'], 'admin')
        self.assertEqual(self.call('GET', '/api/workspaces/admin/knowledge/INDEX.md')['document']['revision'], 1)
        owner = c.execute("SELECT a.handle FROM workspaces w JOIN agents a ON a.id=w.owner_id WHERE w.slug='admin'").fetchone()
        self.assertEqual(owner[0], 'jac')     # the first admin, not the latest

    def test_a_slug_that_cannot_be_adopted_does_not_break_start_or_unlock(self):
        c = db.conn()
        self.drop_admin(c)
        c.execute("UPDATE boards SET kind='conversation' WHERE slug='admin'"); c.commit()
        main._startup()
        self.call('GET', '/api/workspaces/admin', expected=404)

    def test_a_stored_routine_is_paused_and_posts_nothing(self):
        before = db.cursor()
        r = self.add()
        self.assertEqual((r['state'], r['version'], r['problem'], r['prompt'], r['every']),
                         ('paused', 1, '', 'Triage these.', 60))
        self.assertEqual(self.call('GET', f"/api/routines/{r['id']}")['name'], 'triage')
        self.assertEqual([x['id'] for x in self.call('GET', '/api/routines')['routines']], [r['id']])
        self.assertEqual(db.cursor(), before, 'storing a routine posts no message')

    def test_only_humans_and_admin_bots_change_routines(self):
        self.add(auth=self.bot, expected=403)
        r = self.add()
        self.call('POST', f"/api/routines/{r['id']}/pause", auth=self.stranger)     # every human runs the board
        self.call('DELETE', f"/api/routines/{r['id']}", auth=self.bot, expected=403)
        self.make_admin()
        self.call('POST', f"/api/routines/{r['id']}/pause", auth=self.bot)
        self.call('POST', f"/api/routines/{r['id']}/enable")          # a manager needs no cited yes
        self.assertEqual(self.call('GET', f"/api/routines/{r['id']}")['state'], 'enabled')

    def test_an_admin_bot_enables_only_on_a_managers_yes_to_the_current_version(self):
        self.make_admin()
        ask = self.post('Every new issue on bot_board should be triaged')
        early = self.post('sounds right', ask['id'])          # before the definition: approves nothing
        r = self.add(auth=self.bot, thread=ask['id'])
        self.assertEqual(r['thread'], ask['id'])
        enable = f"/api/routines/{r['id']}/enable"
        self.call('POST', enable, {}, auth=self.bot, expected=422)
        self.call('POST', enable, {'approved_by': early['id']}, auth=self.bot, expected=422)
        other = self.post('yes', ask['id'], auth=self.bot)    # a yes must come from a human
        self.call('POST', enable, {'approved_by': other['id']}, auth=self.bot, expected=422)
        elsewhere = self.post('yes', channel='lobby')
        self.call('POST', enable, {'approved_by': elsewhere['id']}, auth=self.bot, expected=422)
        yes = self.post('yes, go', ask['id'])
        tested = self.call('POST', f"/api/routines/{r['id']}/test", {'approved_by': yes['id']}, auth=self.bot)
        self.assertTrue(tested['test_pending'])
        on = self.call('POST', enable, {'approved_by': yes['id']}, auth=self.bot)
        self.assertEqual((on['state'], on['approved_by']), ('enabled', yes['id']))
        v2 = self.call('PATCH', f"/api/routines/{r['id']}", {'every': 300}, auth=self.bot)
        self.assertEqual((v2['state'], v2['version'], v2['every'], v2['check']), ('enabled', 2, 300, CHECK))  # the check is the approved one: it runs on (#380)
        v3 = self.call('PATCH', f"/api/routines/{r['id']}", {'check': CHECK + 'exit 0\n'}, auth=self.bot)
        self.assertEqual((v3['state'], v3['version'], v3['waiting']['changed']), ('paused', 3, 'check (1 line)'))
        self.call('POST', enable, {'approved_by': yes['id']}, auth=self.bot, expected=422)   # a yes approves one version
        yes2 = self.post('ok', ask['id'])
        self.assertEqual(self.call('POST', enable, {'approved_by': yes2['id']}, auth=self.bot)['state'], 'enabled')

    def test_a_new_version_can_move_the_yes_to_the_thread_where_the_fix_is_discussed(self):
        self.make_admin()
        ask = self.post('Every new issue on bot_board should be triaged')
        r = self.add(auth=self.bot, thread=ask['id'])
        enable = f"/api/routines/{r['id']}/enable"
        report = self.post(f"routine {r['id']} stopped: gh failed")
        fix = self.post('can you retry gh?', report['id'])
        v2 = self.call('PATCH', f"/api/routines/{r['id']}", {'every': 300, 'thread': fix['id']}, auth=self.bot)
        self.assertEqual((v2['version'], v2['thread']), (2, report['id']))
        old_yes = self.post('yes', ask['id'])                 # the old thread no longer counts
        refused = self.client.post(enable, json={'approved_by': old_yes['id']}, headers=self.bot)
        self.assertEqual(refused.status_code, 422)
        self.assertIn(f"thread {report['id']}", refused.text)  # and the refusal says which one does
        yes = self.post('yes', report['id'])
        self.assertEqual(self.call('POST', enable, {'approved_by': yes['id']}, auth=self.bot)['state'], 'enabled')
        v3 = self.call('PATCH', f"/api/routines/{r['id']}", {'every': 600}, auth=self.bot)
        self.assertEqual(v3['thread'], report['id'])           # without `thread`, it stays where it was
        self.call('PATCH', f"/api/routines/{r['id']}", {'thread': 999999}, auth=self.bot, expected=404)

    def test_a_recipe_stored_before_workspaces_is_read_in_the_current_shape(self):
        r = self.add(recipe={'type': 'github-issues', 'repo': 'o/r', 'workspace': 'admin'}, check=None, prompt=None)
        c = db.conn()
        c.execute("UPDATE routines SET definition=json_remove(json_set(definition,'$.recipe.project','admin'),"
                  " '$.recipe.workspace','$.recipe.escalate') WHERE id=?", (r['id'],))
        c.commit()
        got = self.call('GET', f"/api/routines/{r['id']}")
        self.assertEqual((got['problem'], got['recipe']['workspace']), ('', 'admin'))
        self.assertEqual(self.call('POST', f"/api/routines/{r['id']}/enable")['state'], 'enabled')
        v2 = self.call('PATCH', f"/api/routines/{r['id']}", {'every': 120})
        self.assertEqual((v2['problem'], v2['recipe']['workspace']), ('', 'admin'))

    def test_a_newer_recipe_writer_drafts_upgrades_a_human_approves_in_one_step(self):
        # #242: routines an older release wrote keep running until one human yes upgrades them all
        rv = routines.cli.RECIPE_VERSION
        ids = [self.add(name=f'r{n}', recipe={'type': 'github-issues', 'repo': 'o/r', 'workspace': 'admin'},
                        check=None, prompt=None)['id'] for n in (1, 2)]
        c = db.conn()
        c.execute("UPDATE routines SET definition=json_set(definition,'$.recipe_version',?,"
                  " '$.check', json_extract(definition,'$.check') || '# older writer' || char(10))", (rv - 1,))
        c.commit()
        self.call('POST', f"/api/routines/{ids[0]}/enable")
        got = self.call('GET', f"/api/routines/{ids[0]}")
        self.assertEqual((got['state'], got['problem'], got['upgrade']['from'], got['upgrade']['to']), ('enabled', '', rv - 1, rv))
        self.assertIn('-# older writer', got['upgrade']['diff'])
        bell = [a['text'] for a in self.call('GET', '/api/status')['attention']]
        self.assertIn(f'3 routine changes wait for your yes: r2 v1, r1 (recipe v{rv}), r2 (recipe v{rv})', bell)  # r2 was never approved

        shown = {str(i): 1 for i in ids}
        self.make_admin()
        self.call('POST', '/api/routines/upgrades/approve', {'recipe_version': rv, 'routines': shown}, auth=self.bot, expected=403)
        self.call('POST', '/api/routines/upgrades/approve', {'recipe_version': rv - 1, 'routines': shown}, expected=422)
        self.call('PATCH', f"/api/routines/{ids[1]}", {'every': 120})      # changed since it was shown: left alone
        out = self.call('POST', '/api/routines/upgrades/approve', {'recipe_version': rv, 'routines': shown})
        self.assertEqual((out['upgraded'], out['left']), ([ids[0]], [{'id': str(ids[1]), 'why': 'changed since you looked'}]))
        up = self.call('GET', f"/api/routines/{ids[0]}")
        self.assertEqual((up['state'], up['version'], up['recipe_version'], up['problem']), ('enabled', 2, rv, ''))
        self.assertNotIn('upgrade', up)
        self.assertEqual(up['check'], routines.cli.recipe_check(up['recipe']))
        self.assertNotIn('upgrade', self.call('GET', f"/api/routines/{ids[1]}"), 'a new version is written by this release')

    def thread_bodies(self, tid):
        return [(m['author'], m['body'], m['meta']) for m in self.call('GET', f'/api/threads/{tid}')['messages']]

    def test_a_bots_version_waits_for_one_click_which_tests_it_and_switches_it_back_on(self):
        # #380: the board asks the human, the human approves on /routines, the runner tests, the board enables
        self.make_admin()
        ask = self.post('Every new issue on bot_board should be triaged')
        r = self.add(auth=self.bot, thread=ask['id'])
        self.assertEqual((r['state'], r['waiting']['changed'], r['waiting']['resume'], r['waiting']['by']), ('paused', 'new', True, 'ada'))  # new: on once approved
        (asked,) = [m for m in self.thread_bodies(ask['id']) if m[0] == 'board']
        self.assertIn('@jac version 1 of routine `triage` (new) waits for your yes', asked[1])
        self.assertEqual((asked[2]['needs'], asked[2]['routine']), ('jac', r['id']))
        inbox = self.call('GET', '/api/status')
        self.assertEqual([(m['where'], m['text']) for m in inbox['waiting'] if m['href'] == f"/t/{ask['id']}"],
                         [('@board', 'approve version 1 of routine triage on /routines#approvals')])
        bell = [a for a in self.call('GET', '/api/status')['attention'] if a['href'] == '/routines#approvals']
        self.assertEqual(bell[0]['text'], '1 routine change waits for your yes: triage v1')

        path = f"/api/routines/{r['id']}"
        self.call('POST', path + '/approve', auth=self.bot, expected=403)       # a bot cannot say yes to itself
        self.call('POST', path + '/enable', auth=self.bot, expected=422)        # nor switch it on without one
        on = self.call('POST', path + '/enable')                                # the human's Enable is a yes
        self.assertEqual((on['state'], 'waiting' in on), ('enabled', False))
        self.assertTrue(on['approved_by'])
        said = self.thread_bodies(ask['id'])[-1]
        self.assertEqual((said[0], said[1], said[2]['resolved']), ('board', 'Version 1 approved by jac. Enabled.', True))
        self.assertFalse(any(m['href'] == f"/t/{ask['id']}" for m in self.call('GET', '/api/status')['waiting']))
        self.assertEqual([a for a in self.call('GET', '/api/status')['attention'] if a['href'] == '/routines#approvals'], [])

        # a new check pauses it and asks again; Approve tests it, and the passing test switches it back on
        v2 = self.call('PATCH', path, {'check': CHECK + '# v2\n'}, auth=self.bot)
        self.assertEqual((v2['state'], v2['waiting']['changed'], v2['waiting']['resume']), ('paused', 'check (1 line)', True))
        self.assertIn('+# v2', v2['waiting']['diff'])
        self.assertIn('and it is enabled again', self.thread_bodies(ask['id'])[-1][1])
        self.call('POST', path + '/test', auth=self.bot, expected=422)          # a bot's test of a waiting version needs the yes
        ok = self.call('POST', path + '/approve')
        self.assertEqual((ok['state'], ok['approving'], ok['test_pending'], 'waiting' in ok), ('paused', True, True, False))
        self.call('POST', path + '/approve', expected=422)                      # once
        self.assertIn('The runner tests it now, then it is enabled again.', self.thread_bodies(ask['id'])[-1][1])
        self.call('POST', path + '/report', {'test': {'for': 1, 'version': 2, 'ok': True, 'work': False}}, auth=self.runner)
        got = self.call('GET', path)
        self.assertEqual((got['state'], got['approving'], got['test_pending']), ('enabled', False, False))
        self.assertEqual(self.thread_bodies(ask['id'])[-1][1], 'Test of version 2 ok, no work right now. Enabled again.')
        self.call('POST', path + '/test', auth=self.bot)                        # approved: no yes needed any more

        # a test that fails leaves it paused and says so
        v3 = self.call('PATCH', path, {'check': CHECK + '# v3\n'}, auth=self.bot)
        self.call('POST', path + '/approve')
        n = self.call('GET', path)['test_asked']
        self.call('POST', path + '/report', {'test': {'for': n, 'version': 3, 'ok': False, 'error': 'exit 1'}}, auth=self.runner)
        got = self.call('GET', path)
        self.assertEqual((got['state'], got['approving'], 'waiting' in got), ('paused', False, False))
        self.assertEqual(self.thread_bodies(ask['id'])[-1][1], '@ada test of version 3 failed: exit 1 It stays paused; a fix is a new version.')  # wakes the bot that stored it
        self.assertEqual(self.call('POST', path + '/enable')['state'], 'enabled')

        # a routine that was paused stays paused after its approval
        self.call('POST', path + '/pause')
        v4 = self.call('PATCH', path, {'check': CHECK + '# v4\n'}, auth=self.bot)
        self.assertFalse(v4['waiting']['resume'])
        self.call('POST', path + '/approve')
        n = self.call('GET', path)['test_asked']
        self.call('POST', path + '/report', {'test': {'for': n, 'version': 4, 'ok': True, 'work': True}}, auth=self.runner)
        self.assertEqual(self.call('GET', path)['state'], 'paused')
        self.assertEqual(self.thread_bodies(ask['id'])[-1][1], 'Test of version 4 ok, found work. It stays paused until someone enables it on /routines.')

    def test_a_change_that_leaves_the_check_alone_keeps_running_and_asks_nobody(self):
        self.make_admin()
        ask = self.post('triage please')
        r = self.add(auth=self.bot, thread=ask['id'])
        path = f"/api/routines/{r['id']}"
        self.call('POST', path + '/enable')
        before = db.cursor()
        v2 = self.call('PATCH', path, {'prompt': 'Triage these, kindly.', 'every': 900, 'name': 'triage2'}, auth=self.bot)
        self.assertEqual((v2['state'], v2['version'], v2['every'], v2['name'], 'waiting' in v2), ('enabled', 2, 900, 'triage2', False))
        self.assertEqual(db.cursor(), before, 'nothing to ask: no message')
        self.call('POST', path + '/test', auth=self.bot)        # no yes to cite, none needed
        # a human's own version waits too, but the board does not write to them about it
        v3 = self.call('PATCH', path, {'check': CHECK + '# human\n'})
        self.assertEqual((v3['state'], v3['waiting']['by']), ('paused', 'jac'))
        self.assertEqual(db.cursor(), before)
        # the version that was waiting is still waiting after a prompt change: the check is still the new one
        v4 = self.call('PATCH', path, {'prompt': 'x'}, auth=self.bot)
        self.assertEqual((v4['state'], v4['waiting']['changed'], v4['waiting']['resume']), ('paused', 'check (1 line), prompt (2 lines)', True))

    def test_a_board_from_before_approvals_counts_what_ran_as_approved(self):
        on, off = self.add(name='on'), self.add(name='off')
        self.call('POST', f"/api/routines/{on['id']}/enable")
        c = db.conn()
        for col in ('approved_definition', 'resume', 'approving'):
            c.execute(f'ALTER TABLE routines DROP COLUMN {col}')
        c.commit()
        routines.init()
        self.assertNotIn('waiting', self.call('GET', f"/api/routines/{on['id']}"))
        self.assertIn('waiting', self.call('GET', f"/api/routines/{off['id']}"))

    def test_delete_stops_it_for_good(self):
        r = self.add()
        self.call('DELETE', f"/api/routines/{r['id']}")
        self.assertEqual(self.call('GET', '/api/routines')['routines'], [])
        self.call('POST', f"/api/routines/{r['id']}/enable", expected=404)
        self.call('DELETE', f"/api/routines/{r['id']}", expected=404)

    def test_run_now_is_for_an_enabled_routine_and_needs_no_new_yes(self):
        # #209: it runs the version already approved
        r = self.add()
        run = f"/api/routines/{r['id']}/run"
        self.call('POST', run, expected=422)                       # paused: enable it first
        self.call('POST', f"/api/routines/{r['id']}/enable")
        self.call('POST', run, auth=self.bot, expected=403)         # not an admin bot yet
        self.make_admin()
        got = self.call('POST', run, auth=self.bot)                 # an admin bot, no approved_by
        self.assertEqual((got['run_pending'], got['run_asked'], got['state']), (True, 1, 'enabled'))
        got = self.call('POST', f"/api/routines/{r['id']}/report", {'run': {'for': 1, 'version': 1, 'result': 'no work'}},
                        auth=self.runner)
        self.assertFalse(got['run_pending'])
        self.assertEqual(got['last_run']['result'], 'no work')
        self.assertIn('at', got['last_run'])

    def test_only_the_runner_reports_and_a_failure_only_fails_the_version_it_ran(self):
        r = self.add()
        report = f"/api/routines/{r['id']}/report"
        self.call('POST', report, {'failed': 'x'}, expected=403)
        self.call('POST', f"/api/routines/{r['id']}/test")
        got = self.call('POST', report, {'test': {'for': 1, 'version': 1, 'ok': True, 'work': False}}, auth=self.runner)
        self.assertFalse(got['test_pending'])
        self.assertEqual(got['last_test']['ok'], True)
        self.call('POST', f"/api/routines/{r['id']}/enable")
        self.call('PATCH', f"/api/routines/{r['id']}", {'every': 90})
        self.call('POST', f"/api/routines/{r['id']}/enable")
        self.assertEqual(self.call('POST', report, {'failed': 'old', 'version': 1}, auth=self.runner)['state'], 'enabled')
        got = self.call('POST', report, {'failed': 'the check exited 1', 'version': 2}, auth=self.runner)
        self.assertEqual((got['state'], got['failure']), ('failed', 'the check exited 1'))

    def test_bad_definitions_are_refused(self):
        self.add(check='  ', expected=422)
        self.add(every=0, expected=422)
        self.add(name=None, expected=422)
        self.add(check='x' * 10, recipe={'type': 'github-issues', 'repo': 'o/r', 'workspace': 'nope'}, expected=404)
        self.add(thread=999999, expected=404)
        self.assertEqual(self.call('GET', '/api/routines')['routines'], [])

    def test_jobs_are_reported_from_the_messages_the_runner_fired(self):
        r = self.add()
        job = self.post('Triage these. found: #1', channel='bot_board', auth=self.runner,
                        meta={'routine': r['id'], 'name': 'triage'})
        self.post('Still open', job['id'], channel='bot_board', auth=self.runner, meta={'routine': r['id'], 'resend': 1})
        self.post('done: labelled #1', job['id'], channel='bot_board', auth=self.bot, meta={'done': True})
        (j,) = self.call('GET', '/api/routines')['routines'][0]['jobs']
        self.assertEqual({k: j[k] for k in ('id', 'sends', 'done_by')}, {'id': job['id'], 'sends': 2, 'done_by': 'ada'})


if __name__ == '__main__':
    unittest.main()
