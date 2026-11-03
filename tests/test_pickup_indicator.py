"""Pickup chips in the live channel and thread renderers."""
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import web


@unittest.skipUnless(shutil.which("node"), "Node required for pickup UI checks")
class PickupIndicator(unittest.TestCase):
    def test_rendering_and_live_worker_refresh(self):
        helpers = web.APP_JS[web.APP_JS.index("function esc("):web.APP_JS.index("function el(")]
        renderer = web.APP_JS[web.APP_JS.index("function pickupHtml("):web.APP_JS.index("function renderHead(")]
        refresh = web.APP_JS[web.APP_JS.index("var refreshingWorkers ="):
                             web.APP_JS.index("setInterval(function(){\n  refreshWorkers()")]
        script = r"""
const assert = require('node:assert/strict');
let S = {thread: 1, roots: [], umsgs: {}}, ME = null;
function av(){ return ''; }
function isHuman(){ return true; }
function when(){ return 'now'; }
function body(text){ return esc(text); }
""" + helpers + renderer + refresh + r"""
const root = {id: 1, author: 'operator', body: 'Investigate this.', tags: [], worker: null};
S.roots = [root];
S.umsgs = {work: [{...root}]};
for (const inThread of [false, true]) {
  assert.doesNotMatch(messageHtml(root, inThread), /Picked up by/);
  const markup = messageHtml({...root, worker: 'atlas'}, inThread);
  assert.equal(markup.split('Picked up by').length - 1, 1);
  assert.match(markup, /data-worker-thread="1"/);
  assert.match(markup, /aria-hidden="true">👀/);
  assert.match(markup, /href="\/a\/atlas">@atlas<\/a>/);
  assert.ok(markup.indexOf('Picked up by') < markup.indexOf('class="foot"'));
  assert.doesNotMatch(messageHtml({...root, reply_to: 1, worker: 'atlas'}, inThread), /Picked up by|data-worker-thread/);
}
assert.doesNotMatch(pickupHtml('bad"><script>'), /<script>/);
assert.match(pickupHtml('bad"><script>'), /&quot;&gt;&lt;script&gt;/);

let writes = 0;
function chip(id) {
  return {dataset: {workerThread: String(id)}, isConnected: true, html: '',
    get innerHTML(){ return this.html; }, set innerHTML(value){ this.html = value; writes++; }};
}
let chips = [chip(1), chip(1)], calls = [], worker = 'atlas', mode = 'ok', downs = {};
let document = {hidden: false, querySelectorAll(){ return chips; }};
async function fetch(url) {
  calls.push(url);
  if (mode === 'http') return {ok: false, status: 503};
  if (mode === 'network') throw new Error('offline');
  if (mode === 'detached') chips[0].isConnected = false;
  const workers = {};
  for (const id of new URL(url, 'http://board').searchParams.getAll('thread')) workers[id] = worker;
  return {ok: true, json: async () => ({workers, down: downs})};
}
async function run() {
  await refreshWorkers();
  assert.deepEqual(calls, ['/api/thread-workers?thread=1']); // channel + thread share one lookup
  assert.equal(writes, 2);
  assert.equal(root.worker, 'atlas');
  assert.equal(S.umsgs.work[0].worker, 'atlas');
  for (const c of chips) assert.match(c.innerHTML, /Picked up by.*@atlas/);
  await refreshWorkers();
  assert.equal(writes, 2); // no duplicate chips or needless DOM writes
  worker = null;
  await refreshWorkers();
  for (const c of chips) assert.equal(c.innerHTML, '');
  assert.equal(root.worker, null);
  worker = 'helper';
  await refreshWorkers();
  for (const c of chips) assert.match(c.innerHTML, /@helper/);
  assert.equal(root.worker, 'helper');

  for (mode of ['http', 'network']) {
    await assert.rejects(refreshWorkers(), /503|offline/);
    assert.equal(refreshingWorkers, false);
    for (const c of chips) assert.match(c.innerHTML, /@helper/); // failed reads do not erase state
  }
  mode = 'detached'; worker = 'other';
  await refreshWorkers();
  assert.match(chips[0].innerHTML, /@helper/); // do not update a view that was navigated away from
  assert.match(chips[1].innerHTML, /@other/);
  mode = 'ok';
  calls = []; document.hidden = true;
  await refreshWorkers();
  assert.equal(calls.length, 0);
  document.hidden = false; chips = [];
  await refreshWorkers();
  assert.equal(calls.length, 0);
  chips = Array.from({length: 205}, (_, i) => chip(i + 1));
  await refreshWorkers();
  assert.equal(calls.length, 2);
  assert.equal(new URL(calls[0], 'http://board').searchParams.getAll('thread').length, 200);
  for (const c of chips) assert.match(c.innerHTML, /@other/);

  downs = {other: 'stopped'}; chips = [chip(1)];
  await refreshWorkers();
  assert.match(chips[0].innerHTML, /class="pickup-chip down" title="This bot is stopped[^"]*">.*@other<\/a> · stopped/);
  assert.match(pickupHtml('other'), /· stopped/);   // a re-render keeps it until the next refresh
  downs = {};
  await refreshWorkers();
  assert.doesNotMatch(chips[0].innerHTML, /stopped|down/);
}
run().catch(e => { console.error(e); process.exitCode = 1; });
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_receipt_labels_and_escaping(self):
        helpers = web.APP_JS[web.APP_JS.index("function esc("):web.APP_JS.index("function el(")]
        renderer = web.APP_JS[web.APP_JS.index("function receiptHtml("):web.APP_JS.index("setInterval(function(){ refreshReceipts()")]
        script = "const assert = require('node:assert/strict');\n" + helpers + renderer + r"""
assert.match(receiptHtml([{handle:'ines', looking:true}]), /👀 Looking.*@ines/);
assert.match(receiptHtml([{handle:'ines', looking:false}]), /✓ Seen.*@ines/);
assert.equal(receiptHtml([]), '');
assert.doesNotMatch(receiptHtml([{handle:'bad"><script>', looking:true}]), /<script>/);
let chips = [1, 1, 2].map(id => ({dataset:{receiptMessage:String(id)}, isConnected:true, innerHTML:''}));
let document = {hidden:false, querySelectorAll:() => chips, querySelector:() => null};
let calls = 0, looking = true;
async function fetch(url){
  calls++;
  assert.equal(url, '/api/message-receipts?message=1&message=2');
  return {ok:true, json: async () => ({receipts:{1:[{handle:'ines', looking}], 2:[]}})};
}
async function check(){
  await refreshReceipts();
  assert.match(chips[0].innerHTML, /Looking/);
  assert.equal(chips[0].innerHTML, chips[1].innerHTML);
  assert.equal(chips[2].innerHTML, '');
  looking = false;
  await refreshReceipts();
  assert.match(chips[0].innerHTML, /Seen/);
  document.hidden = true;
  await refreshReceipts();
  assert.equal(calls, 2);
}
check().catch(e => { console.error(e); process.exitCode = 1; });
"""
        subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
