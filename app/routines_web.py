"""The routines page: what recurring work exists, and a way to run, pause, enable or delete it.

Routines are stored on the board (app/routines.py) and asked for in a
conversation with the admin bot, which stores them. A version whose check no human
has approved waits at the top of this page, with recipe upgrades, for one click
(#380). The page reads GET /api/routines; its writes are Approve (POST /api/routines/{id}/approve
and /api/routines/upgrades/approve), Run now, Pause and Enable (POST /api/routines/{id}/run,
/pause and /enable) and Delete (DELETE /api/routines/{id}),
with the identity the board page keeps in this browser (localStorage bb.me).
Data from the API is only ever put on the page as text, never as HTML.
"""
from __future__ import annotations

import json

from .routines import ADMIN_PROJECT
from .web import shell

ROUTINES_CSS = """
.rt .bar{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;margin:8px 0 14px;font-size:13px;color:var(--dim)}
.rt .badge{display:inline-block;border-radius:4px;padding:0 6px;font:11px var(--mono);background:var(--line);color:var(--dim);margin-left:6px;vertical-align:middle}
.rt .badge.enabled{background:var(--good);color:#fff}.rt .badge.paused{background:var(--line);color:var(--ink)}
.rt .badge.failed{background:var(--accent);color:#fff}.rt .badge.invalid{background:var(--warn);color:#000}
.rt section{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:14px 16px;margin:0 0 14px}
.rt section h3{margin:0 0 8px;font-size:15px}
.rt form{display:grid;gap:8px;margin:6px 0 0}
.rt label{display:grid;gap:3px;font-size:12px;color:var(--dim);font-family:var(--mono)}
.rt input[type=text],.rt input[type=number],.rt textarea{background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:6px 8px;font:13px/1.45 var(--mono);width:100%}
.rt input.short{width:9em}
.rt textarea{min-height:8em;resize:vertical}
.rt textarea.code{min-height:14em;white-space:pre;tab-size:4}
.rt button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:6px 13px;font:600 12px var(--mono);cursor:pointer;justify-self:start}
.rt button.quiet{background:var(--accent-soft);color:var(--accent)}
.rt button.quiet+button.quiet{margin-left:6px}
.rt button:disabled{opacity:.5;cursor:default}
.rt .row{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:flex-end}
.rt .note{font-size:12px;color:var(--dim)}
.rt .wait{border-top:1px solid var(--line);padding:8px 0 4px}
.rt .wait .row{align-items:center}.rt .wait .row a{font-weight:600}
.rt #approvals .row h3{margin:0;flex:1}
.rt .msg-ok{color:var(--good);font-size:13px}.rt .msg-err{color:var(--accent);font-size:13px;white-space:pre-wrap}
.rt .prob{color:var(--accent);font-size:13px;white-space:pre-wrap;margin:6px 0}
.rt pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:4px 0 10px}
.rt table{width:100%;border-collapse:collapse;font-size:13px}
.rt td,.rt th{padding:6px 8px 6px 0;border-top:1px solid var(--line);text-align:left;vertical-align:top}
.rt th{font:600 12px var(--mono);color:var(--dim)}
.rt td.mono{font-family:var(--mono);font-size:12px}
.rt dl{display:grid;grid-template-columns:max-content 1fr;gap:3px 14px;font-size:13px;margin:6px 0 10px}
.rt dt{color:var(--dim);font-family:var(--mono);font-size:12px}.rt dd{margin:0}
.rt details summary{cursor:pointer;color:var(--dim);font-size:13px}
.rt h4{margin:10px 0 0;font:600 11.5px var(--mono);text-transform:uppercase;letter-spacing:.09em;color:var(--dim)}
.rt .srcs{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px}
.rt .src{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:999px;padding:4px 12px;font:500 13px inherit}
.rt .src[aria-pressed=true]{border-color:var(--accent);background:var(--accent-soft);color:var(--accent);font-weight:600}
.rt .src:disabled{opacity:.4}
.rt .checks{display:grid;gap:6px}
.rt label.inline{display:flex;gap:8px;align-items:center;font:13px inherit;color:var(--ink)}
.rt .groups{display:flex;flex-wrap:wrap;gap:6px}
.rt .groups span{font:12px var(--mono);background:var(--line);border-radius:5px;padding:2px 8px}
.rt .rules{display:grid;gap:6px}
.rt .rule{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px}
.rt .rule button{padding:2px 9px}
.rt select{background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:5px 8px;font:13px var(--mono)}
"""

ROUTINES_JS = r"""
(function () {
'use strict';
var ME = null; try { ME = JSON.parse(localStorage.getItem('bb.me')); } catch (err) {}
var root = document.getElementById('rt');
var ADMIN_PROJECT = '__ADMIN_PROJECT__';

// ---- DOM helpers: data only ever goes in as text --------------------------------
function h(tag, attrs) {
  var el = document.createElement(tag);
  Object.keys(attrs || {}).forEach(function (k) {
    var v = attrs[k];
    if (v == null || v === false) return;
    if (k === 'text') el.textContent = v;
    else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  });
  for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
  return el;
}
function add(el) {   // add(el, child, child, ...): arrays are flattened, null and false skipped
  for (var i = 1; i < arguments.length; i++) {
    var c = arguments[i];
    if (c == null || c === false) continue;
    if (Array.isArray(c)) { add.apply(null, [el].concat(c)); continue; }
    el.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
  }
  return el;
}
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
function badge(text, cls) { return h('span', {'class': 'badge ' + (cls || text), text: text}); }
function when(ts) { var d = new Date(ts); return isNaN(d) ? (ts || '') : d.toLocaleString(); }
function status(el, ok, text) { clear(el); el.className = ok ? 'msg-ok' : 'msg-err'; el.textContent = text; }
function chan(c) { return c ? h('a', {href: '/c/' + encodeURIComponent(c), text: '#' + c}) : null; }
function thread(id, text) { return h('a', {href: '/t/' + encodeURIComponent(id), text: text || ('#' + id)}); }

// ---- durations: seconds -> 90s, 2m, 1h, 1d, 1h30m ---------------------------------
function fmtDur(n) {
  n = +n; if (!isFinite(n) || n < 0) return '';
  if (n === 0) return '0s';
  var out = '', u = [['d', 86400], ['h', 3600], ['m', 60], ['s', 1]];
  u.forEach(function (x) { var q = Math.floor(n / x[1]); if (q) { out += q + x[0]; n -= q * x[1]; } });
  return out;
}

// ---- the API -------------------------------------------------------------------
function api(method, path, body) {
  var headers = {};
  if (body !== undefined) headers['content-type'] = 'application/json';
  if (ME && ME.token) headers['Authorization'] = 'Bearer ' + ME.token;
  return fetch(path, {method: method, headers: headers, body: body === undefined ? undefined : JSON.stringify(body)})
    .then(function (r) {
      return r.text().then(function (t) {
        var d = null; try { d = t ? JSON.parse(t) : {}; } catch (err) { d = {error: t}; }
        return {ok: r.ok, status: r.status, data: d || {}};
      });
    }, function () { return {ok: false, status: 0, data: {error: 'could not reach the board'}}; });
}
function why(res) {
  var d = res.data || {};
  var m = d.error || d.detail || ('refused: ' + res.status);
  if (Array.isArray(m)) m = m.map(function (x) { return (x.loc ? x.loc.slice(1).join('.') + ': ' : '') + (x.msg || JSON.stringify(x)); }).join('; ');
  return typeof m === 'string' ? m : JSON.stringify(m);
}

// ================================================================= the page
var data = null;          // GET /api/routines
var open = null;          // routine id shown, from #r=

function current() { var m = /r=(\d+)/.exec(location.hash); return m ? +m[1] : null; }
function load() {
  return api('GET', '/api/routines').then(function (res) {
    if (!res.ok) { draw(res); return res; }
    data = res.data; draw(); return res;
  });
}
window.addEventListener('hashchange', function () { open = current(); draw(); window.scrollTo && window.scrollTo(0, 0); });

function draw(failed) {
  open = current();
  clear(root);
  add(root, h('div', {'class': 'bar'},
    ME ? h('span', {text: 'Signed in as @' + ME.handle + '.'})
       : h('span', null, 'Read only. To pause, enable or delete a routine, sign in on the ', h('a', {href: '/', text: 'board page'}), ' first, then come back.'),
    h('a', {href: '/workspaces/' + encodeURIComponent(ADMIN_PROJECT), text: 'workspace ' + ADMIN_PROJECT})));
  if (failed) { add(root, h('p', {'class': 'msg-err', text: 'Could not load routines: ' + why(failed)})); return; }
  if (!data) { add(root, h('p', {'class': 'sub', text: 'Loading routines…'})); return; }
  var r = open != null ? (data.routines || []).find(function (x) { return +x.id === open; }) : null;
  if (open != null && !r) {
    add(root, h('p', {'class': 'sub'}, h('a', {href: '#', text: '← all routines'})), h('p', {'class': 'msg-err', text: 'No routine ' + open + '.'}));
    return;
  }
  if (r) detail(r); else list();
}

// ---- the list --------------------------------------------------------------------
function list() {
  add(root, h('h1', {text: 'Routines'}),
    h('p', {'class': 'sub'}, 'Recurring work. A check script runs on a timer at no cost; only when it finds work does a message go to the workspace channel for a bot to claim. ',
      'To get a new routine, or to change one, say what you want in the channel of the ',
      h('a', {href: '/workspaces/' + encodeURIComponent(ADMIN_PROJECT), text: ADMIN_PROJECT + ' workspace'}),
      ': the admin bot works it out with you there and stores it; you approve it here, and the board tests it and switches it on.'));
  var rs = data.routines || [];
  approvals(rs.filter(function (r) { return r.waiting && !r.problem; }), rs.filter(function (r) { return r.upgrade; }));
  if (!rs.length) add(root, h('div', {'class': 'empty', text: 'No routine yet.'}));
  else add(root, h('table', null,
    h('thead', null, h('tr', null, ['routine', 'state', 'fires in', 'every', 'jobs', 'asked in'].map(function (t) { return h('th', {text: t}); }))),
    h('tbody', null, rs.map(function (r) {
      var jobs = r.jobs || [], last = jobs[0];
      var openJob = jobs.find(function (j) { return !j.done_at; });
      return h('tr', null,
        h('td', null, h('a', {href: '#r=' + r.id, text: r.name || ('#' + r.id)}), r.about ? h('div', {'class': 'note', text: r.about}) : null),
        h('td', null, badge(r.state || '?', r.state)),
        h('td', null, chan(r.channel)),
        h('td', {'class': 'mono', text: fmtDur(r.every)}),
        h('td', {'class': 'note'}, openJob ? ['open: ', thread(openJob.id)] : last ? ['last: ', thread(last.id), last.done_by ? ' by @' + last.done_by : ''] : 'none yet'),
        h('td', null, r.thread ? thread(r.thread) : null));
    }))));
}

// ---- what waits for a yes: versions a bot stored, and recipe upgrades (#380, #242) ----
// One list, one Approve per item, Approve all at the top. A version's Approve has the runner
// test it, and the board enables it again when the test passes if it was on before the change.
function approvals(waiting, ups) {
  if (!waiting.length && !ups.length) return;
  var to = ups.length ? ups[0].upgrade.to : null, out = h('div', {role: 'status', 'aria-live': 'polite'});
  var n = waiting.length + ups.length;
  function approveVersion(r) {
    return api('POST', '/api/routines/' + encodeURIComponent(r.id) + '/approve').then(function (res) {
      return res.ok ? null : (r.name || '#' + r.id) + ': ' + why(res);
    });
  }
  function approveUpgrades(list) {
    if (!list.length) return Promise.resolve(null);
    var shown = {}; list.forEach(function (r) { shown[r.id] = r.version; });
    return api('POST', '/api/routines/upgrades/approve', {recipe_version: to, routines: shown}).then(function (res) {
      if (!res.ok) return why(res);
      var left = res.data.left || [];
      return left.length ? 'left as they were: ' + left.map(function (x) { return '#' + x.id + ' (' + x.why + ')'; }).join(', ') : null;
    });
  }
  function after(btn, problems) {
    btn.disabled = false;
    problems = problems.filter(Boolean);
    status(out, !problems.length, problems.length ? problems.join('; ') : 'Approved. The runner tests each one within about a minute; the thread says what came of it.');
    load();
  }
  function item(r, what, resume, diff, approve) {
    return h('div', {'class': 'wait'},
      h('div', {'class': 'row'}, h('a', {href: '#r=' + r.id, text: r.name || ('#' + r.id)}), h('span', {'class': 'note', text: what}),
        badge(r.state || '?', r.state), resume ? h('span', {'class': 'note', text: 'on again once the test passes'}) : null,
        ME ? h('button', {type: 'button', 'class': 'quiet', text: 'Approve', onclick: function (ev) {
          var btn = ev.currentTarget; btn.disabled = true;
          approve().then(function (p) { after(btn, [p]); });
        }}) : null),
      diff ? h('details', null, h('summary', {text: 'diff'}), h('pre', {text: diff})) : null,
      r.thread ? h('div', {'class': 'note'}, 'asked in ', thread(r.thread)) : null);
  }
  add(root, h('section', {id: 'approvals'},
    h('div', {'class': 'row'}, h('h3', {text: n + ' routine change' + (n === 1 ? '' : 's') + ' wait' + (n === 1 ? 's' : '') + ' for your yes'}),
      ME && n > 1 ? h('button', {type: 'button', text: 'Approve all', onclick: function (ev) {
        var btn = ev.currentTarget; btn.disabled = true;
        Promise.all(waiting.map(approveVersion).concat([approveUpgrades(ups)])).then(function (ps) { after(btn, ps); });
      }}) : null),
    h('p', {'class': 'note', text: 'A check is code that runs on the runner\'s box with the fleet\'s credentials, so a new one waits for you. ' +
      'Approving has the runner test it; a routine that was on comes back on when the test passes, one that was paused stays paused until you enable it. ' +
      'Changes to a prompt or a setting never wait.'}),
    waiting.map(function (r) {
      var w = r.waiting;
      return item(r, 'version ' + r.version + (w.by ? ' by @' + w.by : '') + ': ' + w.changed, w.resume, w.diff, function () { return approveVersion(r); });
    }),
    ups.map(function (r) {
      return item(r, 'recipe upgrade v' + r.upgrade.from + ' → v' + r.upgrade.to + ': this release writes the check and prompt again, settings kept',
        false, r.upgrade.diff, function () { return approveUpgrades([r]); });
    }),
    ME ? null : h('p', {'class': 'note', text: 'Sign in as a manager of the ' + ADMIN_PROJECT + ' workspace to approve.'}), out));
}

// ---- one routine -----------------------------------------------------------------
function detail(r) {
  add(root, h('p', {'class': 'sub'}, h('a', {href: '#', text: '← all routines'})),
    h('h1', null, r.name || ('#' + r.id), badge(r.state || '?', r.state)),
    r.about ? h('p', {'class': 'sub', text: r.about}) : null);
  if (r.problem) add(root, h('p', {'class': 'prob', text: 'Invalid, so it never runs: ' + r.problem}));
  if (r.upgrade) add(root, h('p', {'class': 'note'}, 'An upgrade to recipe version ' + r.upgrade.to + ' waits for your yes: ',
    h('a', {href: '#approvals', text: 'see it with the others'}), '.'));
  if (r.waiting && !r.problem) add(root, h('p', {'class': 'prob'}, 'Version ' + r.version + ' (' + r.waiting.changed + ') waits for your yes' +
    (r.approving ? ', and the runner is testing it now.' : ': '), r.approving ? null : h('a', {href: '#approvals', text: 'approve it with the others'}), '.'));
  if (r.failure) add(root, h('p', {'class': 'prob', text: 'Failed: ' + r.failure + (r.state === 'failed' ? ' It runs again once enabled again.' : '')}));

  var sec = h('section', null,
    h('dl', null,
      r.thread ? [h('dt', {text: 'asked in'}), h('dd', null, thread(r.thread))] : null,
      h('dt', {text: 'version'}), h('dd', {text: String(r.version) + (r.updated_at ? ', ' + when(r.updated_at) : '')}),
      h('dt', {text: 'fires in'}), h('dd', null, chan(r.channel)),
      h('dt', {text: 'checks every'}), h('dd', {text: fmtDur(r.every)}),
      h('dt', {text: 'claim within'}), h('dd', {text: fmtDur(r.claim_timeout)}),
      h('dt', {text: 'done within'}), h('dd', {text: fmtDur(r.done_timeout)}),
      h('dt', {text: 'sent again'}), h('dd', {text: (r.resends != null ? r.resends : '?') + ' time(s) before it counts as failed'}),
      r.skill ? [h('dt', {text: 'needs skill'}), h('dd', {text: r.skill})] : null),
    h('p', {'class': 'note'}, 'To change it, ask the admin bot', r.thread ? [' in ', thread(r.thread, 'the thread it came from')] : null, '.'));
  add(root, sec);
  if (ME) {
    var out = h('div', {role: 'status', 'aria-live': 'polite'});
    // pause keeps the routine and its history; delete stops it for good
    function act(text, method, path, ask, done) {
      return h('button', {type: 'button', 'class': 'quiet', text: text, onclick: function (ev) {
        if (ask && !window.confirm(ask)) return;
        var btn = ev.currentTarget; btn.disabled = true;
        api(method, path).then(function (res) {
          btn.disabled = false;
          if (!res.ok) { status(out, false, why(res)); return; }
          done();
        });
      }});
    }
    var path = '/api/routines/' + encodeURIComponent(r.id);
    add(sec, r.state === 'enabled' ? act('Run now', 'POST', path + '/run', null, load) : null,
      r.state === 'enabled' ? act('Pause', 'POST', path + '/pause', null, load) : null,
      r.state !== 'enabled' && !r.problem && !r.waiting ? act('Enable', 'POST', path + '/enable', null, load) : null,
      r.waiting && !r.problem && !r.approving ? act('Approve', 'POST', path + '/approve', null, load) : null,   // a yes first; the test switches it on (#380)
      act('Delete', 'DELETE', path, 'Delete the routine ' + (r.name || '#' + r.id) + '? It stops for good.',
          function () { location.hash = ''; load(); }), out);
  }
  add(sec, runResult(r), testResult(r));

  var jobs = r.jobs || [];
  add(root, h('section', null, h('h3', {text: 'Latest jobs'}), jobs.length ? h('table', null,
    h('thead', null, h('tr', null, ['job', 'posted', 'sent', 'done'].map(function (t) { return h('th', {text: t}); }))),
    h('tbody', null, jobs.map(function (j) {
      return h('tr', null, h('td', null, thread(j.id), j.channel ? [' in ', chan(j.channel)] : null),
        h('td', {'class': 'note', text: when(j.created_at)}),
        h('td', {'class': 'mono', text: String(j.sends != null ? j.sends : '')}),
        h('td', {'class': 'note', text: j.done_at ? 'by @' + (j.done_by || '?') + ', ' + when(j.done_at) : 'open'}));
    }))) : h('p', {'class': 'note', text: 'Nothing fired yet.'})));

  if (r.recipe) add(root, recipeView(r));
  var code = [h('h3', {text: 'Prompt'}), h('pre', {text: r.prompt || ''}),
    h('h3', {text: 'Check'}), h('pre', {text: r.check || ''}),
    h('p', {'class': 'note', text: 'The check runs on the runner\'s box with its credentials.'})];
  add(root, h('section', null, r.recipe ? h('details', null, h('summary', {text: 'See the prompt and the check this recipe writes'}), code) : code));
}

function runResult(r) {
  if (r.run_pending) return h('p', {'class': 'note', text: 'Run now asked: the runner checks on its next pass, within about a minute.'});
  var t = r.last_run;
  if (!t) return null;
  var bad = /^(check failed|failed)/.test(t.result || '');
  return h('div', null, h('h3', {text: 'Last run now'}),
    h('p', {'class': bad ? 'msg-err' : 'msg-ok', text: (t.result || '?') + (t.at ? ', ' + when(t.at) : '') +
      (t.version ? ' (version ' + t.version + ')' : '')}));
}

function testResult(r) {
  var t = r.last_test;
  if (!t) return null;
  var found = t.found;
  if (Array.isArray(found)) found = found.join('\n');
  else if (found != null && typeof found !== 'string') found = JSON.stringify(found, null, 1);
  return h('div', null, h('h3', {text: 'Last test'}),
    h('p', {'class': t.ok ? 'msg-ok' : 'msg-err', text: (t.ok ? (t.work ? 'Found work.' : 'Ran fine, nothing to do.') : 'The check failed.') +
      (t.at ? ' ' + when(t.at) : '') + (t.version ? ' (version ' + t.version + ')' : '')}),
    t.error ? h('pre', {'class': 'prob', text: t.error}) : null,
    found ? [h('div', {'class': 'note', text: 'Found:'}), h('pre', {text: found})] : null,
    t.watermark != null ? [h('div', {'class': 'note', text: 'Watermark it would carry:'}), h('pre', {text: JSON.stringify(t.watermark)})] : null);
}

function recipeView(r) {
  var rc = r.recipe;
  return h('section', null, h('h3', {text: 'What it does'}),
    h('dl', null,
      h('dt', {text: 'watches'}), h('dd', null, h('a', {href: 'https://github.com/' + rc.repo + '/issues', text: rc.repo}),
        rc.after ? ', opened after ' + when(rc.after) : ', including the backlog', ', ' + rc.batch + ' at a time'),
      h('dt', {text: 'skips'}), h('dd', {text: [rc.unassigned ? 'assigned' : '', rc.no_linked_pr ? 'linked to a PR' : '',
        rc.skip_labels.length ? 'labelled ' + rc.skip_labels.join(', ') : ''].filter(Boolean).join(' · ') || 'nothing'}),
      h('dt', {text: 'routes to'}), h('dd', null, h('a', {href: '/workspaces/' + encodeURIComponent(rc.workspace || rc.project), text: rc.workspace || rc.project}),
        rc.rules.length ? ' · rules: ' + rc.rules.map(function (x) { return x.label + ' → ' + (x.channel || x.group); }).join(', ') : ''),
      h('dt', {text: 'chases'}), h('dd', {text: (rc.reminds ? 'again after ' + fmtDur(rc.remind_after) + ', ' + rc.reminds + ' times' +
        (rc.escalate ? ', then tells @' + rc.escalate : '') : 'no reminders') +
        (rc.stale_after ? ' · offers again a holder that stops or is silent ' + fmtDur(rc.stale_after) : '')})),
    (r.items || []).length ? [h('h3', {text: 'Announced'}), h('table', null,
      h('thead', null, h('tr', null, ['issue', 'where', 'announced', 'replies', 'held by', 'their last post'].map(function (t) { return h('th', {text: t}); }))),
      h('tbody', null, r.items.map(function (i) {
        return h('tr', null, h('td', null, h('a', {href: 'https://github.com/' + rc.repo + '/issues/' + i.number, text: '#' + i.number})),
          h('td', null, thread(i.id, '#' + i.channel)), h('td', {'class': 'note', text: when(i.created_at)}),
          h('td', {'class': 'mono', text: String(i.replies)}),
          h('td', {'class': 'note', text: i.worker ? i.worker + ' since ' + when(i.worker_claimed_at) : 'nobody'}),
          h('td', {'class': 'note', text: i.worker_last_post ? when(i.worker_last_post) : (i.worker ? 'none yet' : '')}));
      })))] : h('p', {'class': 'note', text: 'Nothing announced yet.'}));
}

draw();
load();
})();
"""


def routines_page(board_name: str) -> str:
    """The whole page for /routines; the script fills it in from /api/routines."""
    return shell(board_name, "routines", here="/routines", content=
                 '<style>' + ROUTINES_CSS + '</style>'
                 '<div class="rt" id="rt"><p class="sub">Loading routines…</p>'
                 '<noscript><p class="sub">This page needs JavaScript. The same data is at '
                 '<a href="/api/routines">/api/routines</a>.</p></noscript></div>'
                 '<script>' + ROUTINES_JS.replace("'__ADMIN_PROJECT__'", json.dumps(ADMIN_PROJECT)) + '</script>')
