"""#223, #445: what waits on a human: messages a bot marked as needing them and their other mentions,
in the board's Mentions view, and paused routines nobody has said yes to, on the bell.
Run with python -m unittest discover -s tests."""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import db, main


class Waiting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, 'board.db')
        db._local = threading.local()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.jac = self.register('jac', 'human')
        self.bot = self.register('tarzan', 'claude-code', workspace='admin')
        for r in self.client.get('/api/routines').json()['routines']:   # the shipped routines (docs/routines) are tests/test_routines_seed.py's subject
            self.client.delete(f"/api/routines/{r['id']}", headers=self.jac)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, 'conn'):
            db._local.conn.close()
        self.tmp.cleanup()

    def register(self, handle, kind, **more):
        r = self.client.post('/api/agents', json={'handle': handle, 'kind': kind, **more})
        self.assertEqual(r.status_code, 201, r.text)
        return {'Authorization': 'Bearer ' + r.json()['token']}

    def post(self, who, body, **more):
        r = self.client.post('/api/messages', headers=who, json={'channel': 'admin', 'body': body, **more})
        self.assertEqual(r.status_code, 201 if r.status_code == 201 else 200, r.text)
        return r.json()['message']['id']

    def status(self, who=None):
        r = self.client.get('/api/status', headers=self.jac if who is None else who)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_marked_ask_waits_until_the_human_replies(self):
        ask = self.post(self.bot, '@jac which repo should this go to?',
                        meta={'needs': 'jac', 'ask': 'pick the repo for the importer'})
        st = self.status()
        self.assertEqual([(w['key'], w['text'], w['where']) for w in st['waiting']],
                         [(f'm{ask}', 'pick the repo for the importer', '@tarzan')])
        self.assertEqual(st['mentions'], [], 'a waiting item is not listed twice')
        self.assertNotIn('waiting', self.status({})), 'only a signed-in human gets the lists'
        self.post(self.jac, 'bot_board', reply_to=ask)
        self.assertEqual(self.status()['waiting'], [])

    def test_the_bot_can_resolve_it_and_the_human_can_dismiss_it(self):
        a = self.post(self.bot, 'need a yes', meta={'needs': ['@jac'], 'ask': 'say yes'})
        self.post(self.bot, 'never mind, found it', reply_to=a, meta={'resolved': True})
        self.assertEqual(self.status()['waiting'], [])
        b = self.post(self.bot, 'need another yes', meta={'needs': 'jac'})
        r = self.client.post('/api/status/dismissed', headers=self.jac, json={'key': f'm{b}'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['waiting'], [])
        self.assertEqual(self.status()['waiting'], [], 'a dismissal of a message is not forgotten as a stale alert')

    def test_a_needs_human_tag_with_a_mention_waits_too(self):
        m = self.post(self.bot, '@jac please review PR 12', tags=['needs-human'])
        self.assertEqual([w['key'] for w in self.status()['waiting']], [f'm{m}'])

    def test_plain_mentions_are_listed_apart_until_answered(self):
        m = self.post(self.bot, '@jac done, merged')
        st = self.status()
        self.assertEqual(st['waiting'], [])
        self.assertEqual([x['key'] for x in st['mentions']], [f'm{m}'])
        self.post(self.jac, 'thanks', reply_to=m)
        self.assertEqual(self.status()['mentions'], [])

    def test_a_paused_routine_nobody_said_yes_to_is_an_alert(self):
        root = self.post(self.bot, 'a routine for triage')
        r = self.client.post('/api/routines', headers=self.jac, json={
            'name': 'triage', 'channel': 'admin', 'every': 600, 'check': '#!/bin/sh\necho {}\n', 'prompt': 'go',
            'thread': root})
        self.assertEqual(r.status_code, 201, r.text)
        item = next(a for a in self.status()['attention'] if a['where'] == 'routines')
        self.assertEqual(item['text'], '1 routine change waits for your yes: triage v1')
        self.assertEqual(item['href'], '/routines#approvals')     # where the yes is given (#380)
        rid = r.json()['id']
        self.client.post(f'/api/routines/{rid}/approve', headers=self.jac)       # a human said yes: it waits no more
        self.assertFalse([a for a in self.status()['attention'] if 'waits for your yes' in a['text']])

    def test_mentions_are_in_the_rail_and_the_bell_keeps_alerts(self):
        # #445: a Mentions view under Unread, what waits on you first; the bell counts alerts only
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Install Playwright and Chromium for board page checks")
        ask = self.post(self.bot, '@jac review PR 9?', meta={'needs': 'jac', 'ask': 'review PR 9'})
        fyi = self.post(self.bot, '@jac merged, fyi')
        alerts = len(self.status()['attention'])
        token = self.jac['Authorization'].split()[1]
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda err: errors.append(str(err)))
                def serve(route):
                    path = route.request.url.replace('http://board.test', '')
                    if 'wait=' in path:
                        return route.abort()
                    r = self.client.request(route.request.method, path, content=route.request.post_data,
                                            headers=route.request.headers)
                    route.fulfill(status=r.status_code, body=r.content, content_type=r.headers.get('content-type', 'text/plain'))
                page.route('**/*', serve)
                page.add_init_script("localStorage.setItem('bb.me', JSON.stringify({handle:'jac', token:'%s'}))" % token)
                page.goto('http://board.test/c/admin')
                page.wait_for_function("document.querySelector('#mentionsli .badge').textContent === '2'")
                self.assertEqual(page.inner_text('#bell-count'), str(alerts or ''), 'asks and mentions do not count on the bell')
                page.click('#bell')
                inbox = page.inner_text('#inbox')
                self.assertNotIn('review PR 9', inbox)
                self.assertIn('2 messages name you, 1 waiting on you: Mentions', inbox)
                page.click('#bell')
                page.click('#mentionsli')
                page.wait_for_selector('#stream .mrow')
                self.assertTrue(page.url.endswith('/mentions'))
                groups = page.locator('#stream .ugroup h3').all_inner_texts()
                self.assertEqual([g.split()[0:3] for g in groups], [['Waiting', 'on', 'you'], ['Mentions', '1']])
                self.assertIn('review PR 9', page.inner_text('#stream .ugroup:nth-of-type(1) .ask'))
                self.assertIn('merged, fyi', page.inner_text('#stream .ugroup:nth-of-type(2)'))
                # done clears one without a reply; an answer in the thread clears the other
                page.click(f'[data-done="m{fyi}"]')
                page.wait_for_function("document.querySelector('#mentionsli .badge').textContent === '1'")
                self.assertNotIn('merged, fyi', page.inner_text('#stream'))
                self.post(self.jac, 'looking now', reply_to=ask)
                page.evaluate('window.bbStatusRefresh()')
                page.wait_for_function("document.querySelector('#mentionsli .badge').hidden")
                self.assertIn('You are all caught up', page.inner_text('#stream'))
                # the address opens it directly too
                self.post(self.bot, '@jac one more')
                page.goto('http://board.test/mentions')
                page.wait_for_selector('#stream .mrow')
                self.assertIn('one more', page.inner_text('#stream'))
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_a_mention_that_cannot_be_read_is_fetched_once(self):
        # #445 review: a message refused or deleted after the status was computed is not fetched again and again
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Install Playwright and Chromium for board page checks")
        gone = self.post(self.bot, '@jac this one is refused')
        self.post(self.bot, '@jac this one reads fine')
        token = self.jac['Authorization'].split()[1]
        asked = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda err: errors.append(str(err)))
                def serve(route):
                    path = route.request.url.replace('http://board.test', '')
                    if 'wait=' in path:
                        return route.abort()
                    if path == f'/api/messages/{gone}':
                        asked.append(path)
                        return route.fulfill(status=404, body='{"error": "no such message"}', content_type='application/json')
                    r = self.client.request(route.request.method, path, content=route.request.post_data,
                                            headers=route.request.headers)
                    route.fulfill(status=r.status_code, body=r.content, content_type=r.headers.get('content-type', 'text/plain'))
                page.route('**/*', serve)
                page.add_init_script("localStorage.setItem('bb.me', JSON.stringify({handle:'jac', token:'%s'}))" % token)
                page.goto('http://board.test/mentions')
                page.wait_for_selector('#stream .mrow')
                page.wait_for_timeout(800)
                self.assertEqual(len(asked), 1, 'fetched once, not in a loop')
                self.assertIn('this one reads fine', page.inner_text('#stream'))
                self.assertNotIn('refused', page.inner_text('#stream'))
                self.assertEqual(errors, [])
            finally:
                browser.close()

if __name__ == '__main__':
    unittest.main()
