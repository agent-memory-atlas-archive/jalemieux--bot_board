import unittest
from html.parser import HTMLParser

from app import db, web


class FleetSections(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.sections = {}
        self.stack = []
        self.hidden = 0
        self.section = None
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('section', 'details', 'div'):
            self.stack.append((tag, self.section, self.hidden))
            if attrs.get('id', '').startswith(('fleet-', 'box-')):
                self.section = attrs['id']
            if tag == 'details' and 'open' not in attrs:
                self.hidden += 1

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] == tag:
            _, self.section, self.hidden = self.stack.pop()

    def handle_data(self, data):
        if self.section and not self.hidden:
            self.sections[self.section] = self.sections.get(self.section, '') + data


def bot(handle, status='waiting', **extra):
    return dict(handle=handle, status=status, harness='codex', **extra)


def host(name, bots, reporting=True, **extra):
    return dict(name=name, bots=bots, reporting=reporting, reported_at=db.now(),
                keeper='keeper-' + name, cores=8, load=0.3,
                mem_total_mb=16000, mem_free_mb=12000, **extra)


class FleetPage(unittest.TestCase):
    def test_legacy_keeper_handle_with_update_renders_online_and_offline(self):
        for reporting in (True, False):
            with self.subTest(reporting=reporting):
                page, sections = self.render([host(
                    'legacy', [], reporting=reporting,
                    update={'revision': 'v1.0.124', 'result': 'up to date'})])
                self.assertIn('checkout v1.0.124', sections['fleet-machines'])
                if reporting:
                    self.assertIn('before #238', page)

    def test_health_timestamps_are_localizable_and_diagnostics_stay_escaped(self):
        health = 'out of codex usage since 2026-10-17T06:57:20Z: <script>bad</script>'
        page, _ = self.render([host('live', [bot('limited', health=health)])])
        self.assertEqual(page.count('data-local-time datetime="2026-10-17T06:57:20Z"'), 2)
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;', page)
        self.assertNotIn('<script>bad</script>', page)
        self.assertIn("timeZoneName: 'short'", page)
        self.assertIn('toLocaleString', page)
        self.assertEqual(web._fleet_detail('bad 2026-99-99T06:57:20Z <x>'),
                         'bad 2026-99-99T06:57:20Z &lt;x&gt;')

    def render(self, hosts):
        page = web.fleet_page('board', hosts)
        return page, FleetSections(page).sections

    def test_active_bots_lead_and_counts_match_what_they_do(self):
        page, sections = self.render([
            host('live', [bot('ready'), bot('busy', 'running game'),
                          bot('limited', health='out of codex usage: resets Monday'),
                          bot('login', health='logged out of codex'),
                          bot('leaving', 'running game, stopping'), bot('old', 'stopped')]),
            host('offline', [bot('ghost'), bot('gone', 'stopped')], reporting=False),
        ])
        self.assertIn('Working <b>1</b>', page)
        self.assertIn('Waiting <b>1</b>', page)
        self.assertIn('Needs you · 3', page)     # the usage limit, the login, the box gone dark
        self.assertIn('Stopped <b>1</b>', page)
        self.assertIn('1 not reporting', page)
        bots = sections['fleet-bots']
        # working first, then waiting, then the rest; stopped bots fold away
        self.assertLess(bots.index('@busy'), bots.index('@ready'))
        self.assertLess(bots.index('@ready'), bots.index('@leaving'))
        self.assertNotIn('@old', bots)
        self.assertIn('@old', page)
        # a box gone dark shows what it last ran, not what it had stopped
        self.assertIn('waiting when last heard', bots)
        self.assertNotIn('class="act" data-handle="ghost"', page)     # nothing there would carry a stop out
        self.assertIn('class="act delete-bot" data-handle="ghost"', page)     # a delete needs no box (#280)
        self.assertNotIn('@gone', page)
        self.assertIn('5 up · 1 stopped', sections['fleet-machines'])
        self.assertIn('not reporting', sections['fleet-machines'])
        self.assertIn('offline stopped reporting', sections['fleet-attention'])

    def test_each_row_says_group_harness_machine_and_last_active(self):
        page, sections = self.render([host('live', [
            bot('ded', workspace='game', pool=True, last_run=db.now()),
            bot('shared', pool=True), bot('single', serves='/home/me/src/wordsnap/')])])
        bots = sections['fleet-bots']
        self.assertIn('href="/workspaces/game"', page)
        self.assertIn('no workspace', bots)
        self.assertIn('wordsnap', bots)
        self.assertNotIn('/home/me/src', bots)
        self.assertIn('codex', bots)
        self.assertIn('href="#box-live"', page)
        self.assertIn('0s ago', bots)
        self.assertIn('never', bots)

    def test_usage_alert_is_grouped_and_says_nothing_needs_doing(self):
        reason = 'out of codex usage: weekly limit; resets Monday'
        page, sections = self.render([host('live', [
            bot('one', health=reason),
            bot('two', 'stopped', health=reason, want='running', set_by='op', set_at=db.now()),
        ])])
        attention = sections['fleet-attention']
        self.assertEqual(attention.count('codex usage limit reached'), 1)
        self.assertIn('once the limit resets', attention)
        self.assertIn('@one', attention)
        self.assertIn('@two', attention)
        self.assertIn('Stop them', attention)
        self.assertIn('Working <b>0</b>', page)
        self.assertIn('Waiting <b>0</b>', page)

    def test_controls_sit_on_the_row(self):
        page, sections = self.render([host('live', [
            bot('ready', pool=True, serves='/saved/sessions'),
            bot('retired', 'stopped', health='out of codex usage: old limit', want='stopped',
                set_by='op', set_at=db.now()),
        ]), host('other', [])])
        self.assertNotIn('fleet-attention', sections)
        self.assertIn('Stopped <b>1</b>', page)
        self.assertIn('data-kill="1"', page)
        self.assertIn('class="move" data-handle="ready"', page)
        self.assertNotIn('class="move" data-handle="retired"', page)
        self.assertIn('data-handle="retired" data-host="live" data-run="1"', page)
        self.assertIn('class="drain"', page)

    def test_requested_start_and_failure_stay_in_view_with_a_fix(self):
        page, sections = self.render([host('live', [
            bot('starting', 'stopped', want='running', set_by='op', set_at=db.now()),
            bot('broken', 'stopped', want='running', problem='codex not installed',
                set_by='op', set_at=db.now()),
        ])])
        self.assertIn('@starting', sections['fleet-bots'])
        self.assertIn('start asked by @op', sections['fleet-bots'])
        self.assertIn('Could not start 1 bot', sections['fleet-attention'])
        self.assertIn('codex not installed', sections['fleet-attention'])
        self.assertIn('Try again', sections['fleet-attention'])
        self.assertIn('Leave stopped', sections['fleet-attention'])
        self.assertIn('title="withdraw the start">cancel', page)

    def test_failed_last_run_keeps_worker_available(self):
        page, sections = self.render([host('live', [bot('retry', health='last run failed: temporary error')])])
        self.assertIn('All running <b>1</b>', page)
        self.assertIn('@retry', sections['fleet-bots'])
        self.assertIn('last run failed: temporary error', sections['fleet-bots'])

    def test_credentials_and_empty_machine_states(self):
        page, sections = self.render([host('empty', [], credentials=[
            dict(name='claude', state='missing', detail='Run claude /login'),
        ])])
        self.assertIn('Run claude /login', sections['fleet-attention'])
        # the bots it holds are not one more alert each
        held, held_sections = self.render([host('box', [bot('dex', health='logged out of codex')],
                                           credentials=[dict(name='codex', state='missing', detail='not logged in')])])
        self.assertIn('codex missing on box', held_sections['fleet-attention'])
        self.assertIn('0 up · 0 stopped', sections['fleet-machines'])
        self.assertIn('No bot is running', sections['fleet-bots'])
        self.assertIn('No box has reported yet', self.render([])[0])

    def test_untrusted_names_and_health_are_escaped(self):
        page, _ = self.render([host('<machine>', [bot('<worker>', health='out of usage: <script>bad()</script>')])])
        self.assertNotIn('<script>bad()</script>', page)
        self.assertIn('&lt;script&gt;bad()&lt;/script&gt;', page)
        self.assertIn('&lt;machine&gt;', page)

    def test_add_bot_form_lists_reporting_boxes_harnesses_and_workspaces(self):
        page = web.fleet_page('board', [host('live', [bot('ready')]),
                                        host('dark', [], reporting=False)],
                              workspaces=['falco', 'baby-falco'])
        self.assertIn('+ Add a bot', page)
        self.assertIn('class="newbot-top"', page)
        self.assertIn('<option value="live">live</option>', page)
        self.assertNotIn('<option value="dark">dark</option>', page)
        for harness in ('claude', 'codex', 'opencode', 'copilot'):
            self.assertIn('>' + harness + '<', page)
        self.assertIn('name="workspace"', page)
        self.assertNotIn('shared pool', page)
        self.assertIn('<option value="falco">falco</option>', page)
        self.assertIn('<option value="baby-falco">baby-falco</option>', page)

    def test_add_bot_form_absent_without_a_reporting_box(self):
        page = web.fleet_page('board', [host('dark', [], reporting=False)], workspaces=['falco'])
        self.assertNotIn('class="newbot-top"', page)
        self.assertIn('a machine must be reporting', page)

    def test_add_machine_section_is_present_even_without_hosts(self):
        page, _ = self.render([])
        setup = page[page.index('id="fleet-add-machine"'):]
        self.assertIn('Before you start', setup)
        self.assertIn('Python 3', setup)
        self.assertIn('gh auth login', setup)
        self.assertIn('Open a terminal on that machine', setup)
        self.assertIn('id="machine-copy" disabled', page)
        self.assertIn('id="machine-status"', page)
        self.assertIn('location.origin', web.FLEET_JS)
        self.assertNotIn('KEEPER_OPERATORS', web.FLEET_JS)
        self.assertNotIn('board.example.invalid', page)

    def test_refresh_keeps_details_open_without_hiding_recovery(self):
        import json
        import shutil
        import subprocess
        if not shutil.which('node'):
            self.skipTest('node required for fleet refresh behavior')
        script = web.FLEET_JS[web.FLEET_JS.index('// Preserve expanded worker details'):]
        harness = r'''
const assert = require('node:assert/strict');
let tick, reloads = 0, inputs = [];
const saved = new Map();
global.sessionStorage = {
  getItem: key => saved.get(key),
  setItem: (key, value) => saved.set(key, value),
  removeItem: key => saved.delete(key)
};
const detail = {
  open: true,
  closest: selector => selector === 'tr' ? {querySelector: () => ({getAttribute: () => '/a/olga'})} : {id: 'fleet-running'},
  querySelector: () => ({textContent: 'Details for @olga'})
};
global.document = {
  activeElement: null,
  querySelectorAll: selector => selector.endsWith('input') ? inputs : [detail]
};
global.location = {reload: () => reloads++};
global.setInterval = fn => { tick = fn; };
'''
        checks = r'''
tick();
assert.equal(reloads, 1, 'expanded details must not block recovery refresh');
assert.equal(JSON.parse(saved.get('fleet.expanded')).length, 1);
detail.open = false;
'''
        checks += '\neval(' + json.dumps(script) + ');\n'
        checks += r'''
assert.equal(detail.open, true, 'expanded details restored after refresh');
document.activeElement = {tagName: 'INPUT'};
tick(); assert.equal(reloads, 1, 'active form protected');
document.activeElement = null;
inputs = [{value: 'unsaved bot name'}];
tick(); assert.equal(reloads, 1, 'unsaved form protected');
'''
        subprocess.run(['node', '-e', harness + script + checks], check=True, capture_output=True, text=True)
