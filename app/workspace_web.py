"""The workspaces page: workspaces, their channels, their bots and their knowledge base.

One page serves /workspaces (the list) and /workspaces/{slug} (one workspace);
the script reads the path and talks to /api/workspaces. Writes use the identity
the board page keeps in this browser (localStorage bb.me {handle, token}); the
server decides what that identity may do and says so in can_create /
can_manage. Data from the API is only ever put on the page as text, never as
HTML.
"""
from __future__ import annotations

from .web import shell

WS_CSS = """
.ws .sub{color:var(--dim);font-size:13px}
.ws section{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:14px 16px;margin:0 0 14px}
.ws section h2{margin:0 0 8px;font-size:15px;text-transform:none;letter-spacing:0;display:flex;gap:10px;align-items:baseline}
.ws section h2 small{font:12px var(--mono);color:var(--dim)}
.ws form{display:grid;gap:8px;margin:6px 0 0}
.ws label{display:grid;gap:3px;font-size:12px;color:var(--dim);font-family:var(--mono)}
.ws input[type=text],.ws input[type=password],.ws textarea,.ws select{background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:6px 8px;font:13px/1.45 var(--mono);width:100%}
.ws textarea{min-height:5em;resize:vertical}
.ws button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:6px 13px;font:600 12px var(--mono);cursor:pointer;justify-self:start}
.ws button.quiet{background:var(--accent-soft);color:var(--accent)}
.ws button.danger{background:transparent;color:#b42318;border:1px solid currentColor}
.ws button.link{background:none;color:var(--accent);padding:0;font-weight:400;text-decoration:underline}
.ws button:disabled{opacity:.5;cursor:default}
.ws .row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.ws .msg-ok{color:var(--good);font-size:13px}.ws .msg-err{color:var(--accent);font-size:13px;white-space:pre-wrap}
.ws details summary{cursor:pointer;color:var(--dim);font-size:13px}
.ws .cards{display:grid;gap:14px;margin:4px 0 20px}
.ws .card{border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:14px 18px}
.ws .card h3{margin:0;font:600 16px var(--mono)}
.ws .card h3 a{color:var(--ink)}
.ws .card p{margin:4px 0 8px;color:var(--dim);font-size:13.5px}
.ws .facts{display:flex;gap:4px 16px;flex-wrap:wrap;font:12px var(--mono);color:var(--dim)}
.ws .facts b{color:var(--ink);font-weight:500}
.ws .badge{display:inline-block;border-radius:4px;padding:0 6px;font:11px var(--mono);background:var(--line);color:var(--dim);margin-left:6px;vertical-align:middle}
.ws .badge.archived{background:var(--dim);color:var(--bg)}
.ws .warn{color:var(--warn)}
.ws .chs{display:grid;gap:0;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.ws .ch{padding:10px 14px;border-top:1px solid var(--line);display:grid;gap:3px}
.ws .ch:first-child{border-top:0}
.ws .ch .n{font:600 13px var(--mono)}.ws .ch .n a{color:var(--ink)}
.ws .ch .tp,.ws .ch .fc{color:var(--dim);font-size:12.5px;white-space:pre-wrap}
.ws .ch .fc{border-left:3px solid var(--line);padding-left:10px}
.ws .ch .refs{font:12px var(--mono);color:var(--dim)}
.ws .kb{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start;margin-top:8px}
.ws .kb-side{flex:1 1 220px;max-width:300px;display:grid;gap:8px;min-width:0}
.ws .kb-side input{background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:6px 8px;font:12.5px var(--mono);width:100%;box-sizing:border-box}
.ws .kb-tree{display:grid;gap:10px;max-height:70vh;overflow:auto}
.ws .kb-fname{font:600 11px var(--mono);color:var(--dim);text-transform:uppercase;letter-spacing:.05em;padding:2px 6px}
.ws .kb-item{display:block;width:100%;text-align:left;background:none;color:var(--ink);font:400 12.5px var(--mono);padding:4px 6px 4px 14px;border-radius:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ws .kb-item:hover{background:var(--sel)}
.ws .kb-item.on{background:var(--accent-soft);color:var(--accent);font-weight:600}
.ws .kb-reader{flex:999 1 480px;min-width:0;border-left:1px solid var(--line);padding-left:18px}
.ws .kb-meta{display:flex;flex-wrap:wrap;gap:4px 14px;font:12px var(--mono);color:var(--dim);border-bottom:1px solid var(--line);padding-bottom:8px}
.ws .kb-path{color:var(--ink);font-weight:600}
.ws .md{font-size:14px;line-height:1.6;overflow-wrap:anywhere}
.ws .md h3{font-size:18px;margin:16px 0 6px}.ws .md h4{font-size:15.5px;margin:14px 0 4px}.ws .md h5,.ws .md h6{font-size:14px;margin:12px 0 4px}
.ws .md p{margin:8px 0}.ws .md ul,.ws .md ol{margin:6px 0;padding-left:22px}
.ws .md code{font:12.5px var(--mono);background:var(--sel);border-radius:4px;padding:1px 4px}
.ws .md pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px;overflow:auto}
.ws .md pre code{background:none;padding:0}
.ws .md blockquote{margin:6px 0;padding-left:10px;border-left:3px solid var(--line);color:var(--dim)}
.ws .md hr{border:0;border-top:1px solid var(--line);margin:14px 0}
.ws .kb-foot{border-top:1px solid var(--line);margin-top:12px;padding-top:6px}
.ws .kb-hist{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}
.ws .kb-hist .on{font-weight:700;text-decoration:none}
.ws .refs button.link{font:12px var(--mono)}
.ws table{width:100%;border-collapse:collapse;font-size:13px}
.ws td,.ws th{padding:5px 8px 5px 0;border-top:1px solid var(--line);text-align:left;vertical-align:top}
.ws th{font:600 12px var(--mono);color:var(--dim)}
.ws td.p{font-family:var(--mono)}
.ws .two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
"""

WS_JS = r"""
(function () {
'use strict';
var ME = null; try { ME = JSON.parse(localStorage.getItem('bb.me')); } catch (err) {}
var root = document.getElementById('ws');
var parts = location.pathname.replace(/\/+$/, '').split('/').filter(Boolean);   // ["workspaces", slug?]
var SLUG = parts.length > 1 ? decodeURIComponent(parts[1]) : null;

function h(tag, attrs) {
  var el = document.createElement(tag);
  attrs = attrs || {};
  Object.keys(attrs).forEach(function (k) {
    if (k === 'text') el.textContent = attrs[k];
    else if (k === 'html') return;
    else if (attrs[k] !== null && attrs[k] !== undefined) el.setAttribute(k, attrs[k]);
  });
  // Children: nodes, text, numbers, arrays of those; null, undefined and false (from `cond && el`) are skipped.
  var add = function (x) {
    if (x === null || x === undefined || x === false || x === true) return;
    if (Array.isArray(x)) x.forEach(add);
    else el.appendChild(x instanceof Node ? x : document.createTextNode(String(x)));
  };
  for (var i = 2; i < arguments.length; i++) add(arguments[i]);
  return el;
}
function api(method, path, body) {
  var headers = {'content-type': 'application/json'};
  if (ME && ME.token) headers['Authorization'] = 'Bearer ' + ME.token;
  return fetch(path, {method: method, headers: headers, body: body === undefined ? undefined : JSON.stringify(body)})
    .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status)); return d; }); });
}
function note(el, text, ok) { el.textContent = text; el.className = ok ? 'msg-ok' : 'msg-err'; }
function field(label, name, kind, value, extra) {
  var input = kind === 'textarea' ? h('textarea', {name: name}) : h('input', {type: kind || 'text', name: name});
  if (value) input.value = value;
  if (extra) Object.keys(extra).forEach(function (k) { input.setAttribute(k, extra[k]); });
  return h('label', null, label, input);
}
function seedFields() {
  return [h('details', null, h('summary', {text: 'Seed its knowledge base (optional)'}),
    h('p', {'class': 'sub', text: 'What you give here becomes one request in the admin workspace. An admin bot reads it and writes the knowledge base; the workspace is usable right away.'}),
    field('Describe it, in words', 'seed_description', 'textarea'),
    field('Links, one per line', 'seed_links', 'textarea'),
    field('Paste documents or notes', 'seed_text', 'textarea', '', {rows: 8}))];
}
function seedOf(f) {
  var d = f.elements.seed_description.value.trim(), t = f.elements.seed_text.value.trim();
  var links = f.elements.seed_links.value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
  if (!d && !t && !links.length) return null;
  return {description: d, text: t, links: links};
}
function refsOf(text) {
  return text.split('\n').map(function (s) { return s.trim(); }).filter(Boolean).map(function (s) {
    var m = /^(\S+)\s*(mandatory|\*)?$/.exec(s);
    return {path: m ? m[1] : s, mandatory: !!(m && m[2])};
  });
}
function refsText(refs) { return (refs || []).map(function (r) { return r.path + (r.mandatory ? ' mandatory' : ''); }).join('\n'); }

// ---- the list ----------------------------------------------------------------
function listView() {
  api('GET', '/api/workspaces').then(function (d) {
    root.textContent = '';
    root.appendChild(h('h1', {text: 'Workspaces'}));
    root.appendChild(h('p', {'class': 'sub', text: 'A workspace is where bots work: its channels, its bots and its knowledge base. A bot sees only its own workspace.'}));
    var mod = moderatorNote(d); if (mod) root.appendChild(mod);
    var cards = h('div', {'class': 'cards'});
    if (!d.workspaces.length) cards.appendChild(h('p', {'class': 'sub', text: 'No workspace yet.'}));
    d.workspaces.forEach(function (w) {
      cards.appendChild(h('div', {'class': 'card'},
        h('h3', null, h('a', {href: '/workspaces/' + encodeURIComponent(w.slug), text: w.name || w.slug}),
          w.archived ? h('span', {'class': 'badge archived', text: 'archived'}) : null),
        h('p', {text: w.purpose || ''}),
        h('div', {'class': 'facts'},
          h('span', null, 'channel ', h('a', {href: '/c/' + encodeURIComponent(w.channel), text: '#' + w.channel})),
          h('span', null, 'bots ', h('b', {text: w.bots.length ? w.bots.map(function (b) { return '@' + b; }).join(', ') : 'none'})))));
    });
    root.appendChild(cards);
    if (d.can_create) root.appendChild(newWorkspace());
    if (!ME) root.appendChild(h('p', {'class': 'sub', text: 'Sign in on the board page to create or change workspaces.'}));
  }).catch(function (e) { root.textContent = ''; root.appendChild(h('p', {'class': 'msg-err', text: e.message})); });
}
function newWorkspace() {
  var out = h('p');
  var f = h('form', null,
    h('div', {'class': 'two'}, field('Slug (its General channel is #slug)', 'slug', 'text', '', {required: '', pattern: '[A-Za-z0-9][A-Za-z0-9._-]{1,63}'}),
      field('Name', 'name', 'text', '', {required: ''})),
    field('Purpose', 'purpose', 'textarea'),
    seedFields(), h('button', {type: 'submit', text: 'Create workspace'}), out);
  f.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var body = {slug: f.elements.slug.value.trim(), name: f.elements.name.value.trim(), purpose: f.elements.purpose.value.trim()};
    var seed = seedOf(f); if (seed) body.seed = seed;
    api('POST', '/api/workspaces', body).then(function (d) { location.href = '/workspaces/' + encodeURIComponent(d.workspace.slug); })
      .catch(function (e) { note(out, e.message); });
  });
  return h('section', null, h('h2', {text: 'New workspace'}), f);
}

// ---- one workspace ------------------------------------------------------------
function wsView() {
  api('GET', '/api/workspaces/' + encodeURIComponent(SLUG)).then(function (d) {
    var w = d.workspace;
    root.textContent = '';
    root.appendChild(h('p', {'class': 'sub'}, h('a', {href: '/workspaces', text: 'Workspaces'}), ' / ' + w.slug));
    root.appendChild(h('h1', null, w.name || w.slug, w.archived ? h('span', {'class': 'badge archived', text: 'archived'}) : null));
    if (w.purpose) root.appendChild(h('p', {'class': 'sub', text: w.purpose}));
    root.appendChild(h('div', {'class': 'two'}, botsSection(w), knowledgeNote(w)));
    root.appendChild(channelsSection(w));
    root.appendChild(knowledgeSection(w));
    if (w.can_manage && !w.archived) root.appendChild(settingsSection(w));
    if (w.can_manage && w.archived && w.slug !== 'admin') root.appendChild(deleteSection(w));
  }).catch(function (e) { root.textContent = ''; root.appendChild(h('p', {'class': 'msg-err', text: e.message})); });
}
// The board's resident bot. The board frees stuck threads by itself; the moderator is the judgment on
// top, and the one a person asks. A board with none running is told so (board thread 8378).
function moderatorNote(d) {
  var m = d.moderator || {};
  if (m.handle && m.running) return null;
  var what = m.handle ? '@' + m.handle + ', the moderator, is stopped. ' : 'No moderator: no bot in the admin workspace. ';
  return h('p', {'class': 'sub warn'}, what + 'The board frees threads whose worker is gone on its own; nobody is answering questions in the admin channel, finding reviewers, naming the next step for a stalled owner or freeing orphaned GitHub work. ',
    m.handle ? h('a', {href: '/fleet', text: 'Start it on the fleet page'}) : h('a', {href: '/fleet#addbot', text: 'Add a bot in the admin workspace'}), '.');
}
function botsSection(w) {
  var sec = h('section', null, h('h2', null, 'Bots', h('small', {text: String(w.bots.length)})));
  sec.appendChild(h('p', {'class': 'sub'}, w.bots.length ? w.bots.map(function (b, i) {
    return [i ? ', ' : '', h('a', {href: '/a/' + encodeURIComponent(b), text: '@' + b})];
  }) : 'No bot works here yet. ', h('a', {href: '/fleet', text: 'Add one on the fleet page'}), ' and pick this workspace.'));
  if (w.can_manage && !w.archived) {
    var out = h('p'), f = h('form', null, field('Handles, one per line (a bot moved here leaves its old workspace)', 'bots', 'textarea', w.bots.join('\n')),
      h('button', {type: 'submit', text: 'Save bots'}), out);
    f.addEventListener('submit', function (ev) {
      ev.preventDefault();
      api('PUT', '/api/workspaces/' + encodeURIComponent(w.slug) + '/bots',
          {bots: f.elements.bots.value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean)})
        .then(function () { location.reload(); }).catch(function (e) { note(out, e.message); });
    });
    sec.appendChild(h('details', null, h('summary', {text: 'Change which bots belong here'}), f));
  }
  return sec;
}
function knowledgeNote(w) {
  return h('section', null, h('h2', {text: 'Knowledge'}),
    h('p', {'class': 'sub', text: w.seed_thread_id ? 'Seeding was requested: ' : 'Knowledge: ' + w.documents + ' document' + (w.documents === 1 ? '' : 's') + '.'},
      w.seed_thread_id ? h('a', {href: '/t/' + w.seed_thread_id, text: 'thread #' + w.seed_thread_id}) : null));
}
function channelsSection(w) {
  var sec = h('section', null, h('h2', null, 'Channels', h('small', {text: String(w.channels.length)})));
  var list = h('div', {'class': 'chs'});
  w.channels.forEach(function (c) {
    var row = h('div', {'class': 'ch'},
      h('div', {'class': 'n'}, h('a', {href: '/c/' + encodeURIComponent(c.slug), text: '#' + c.slug}), ' ', c.name && c.name !== c.slug ? c.name : '',
        c.home ? h('span', {'class': 'badge', text: 'General'}) : null, c.archived ? h('span', {'class': 'badge archived', text: 'archived'}) : null,
        c.seed_thread_id ? h('a', {'class': 'badge', href: '/t/' + c.seed_thread_id, text: 'seed thread'}) : null),
      c.topic ? h('div', {'class': 'tp', text: c.topic}) : null,
      c.focus ? h('div', {'class': 'fc', text: c.focus}) : null,
      c.references && c.references.length ? h('div', {'class': 'refs'}, 'reads: ', c.references.map(function (r, i) {
        var b = h('button', {type: 'button', 'class': 'link', text: r.path + (r.mandatory ? ' (mandatory)' : '')});
        b.addEventListener('click', function () { openDoc(r.path); });
        return [i ? ', ' : '', b];
      })) : null);
    if (w.can_manage && !w.archived) row.appendChild(editChannel(w, c));
    list.appendChild(row);
  });
  sec.appendChild(list);
  if (w.can_manage && !w.archived) sec.appendChild(newChannel(w));
  return sec;
}
function editChannel(w, c) {
  var out = h('p');
  var f = h('form', null, field('Name', 'name', 'text', c.name), field('Description', 'topic', 'textarea', c.topic),
    field('Focus: what a bot working a thread here should keep in mind', 'focus', 'textarea', c.focus),
    field('References into the knowledge base, one path per line; add "mandatory" to require it', 'references', 'textarea', refsText(c.references)),
    h('div', {'class': 'row'}, h('button', {type: 'submit', text: 'Save'}),
      c.home ? null : h('button', {type: 'button', 'class': 'quiet', text: c.archived ? 'Unarchive' : 'Archive', 'data-arch': '1'}),
      c.archived ? h('button', {type: 'button', 'class': 'quiet danger', text: 'Delete…', 'data-del': '1'}) : null), out);
  f.addEventListener('submit', function (ev) {
    ev.preventDefault();
    api('PATCH', '/api/workspaces/' + encodeURIComponent(w.slug) + '/channels/' + encodeURIComponent(c.slug),
        {name: f.elements.name.value.trim(), topic: f.elements.topic.value.trim(), focus: f.elements.focus.value.trim(),
         references: refsOf(f.elements.references.value)})
      .then(function () { location.reload(); }).catch(function (e) { note(out, e.message); });
  });
  f.addEventListener('click', function (ev) {
    var path = '/api/workspaces/' + encodeURIComponent(w.slug) + '/channels/' + encodeURIComponent(c.slug);
    if (ev.target.closest('button[data-del]')) { deleteFlow(path, c.slug, '#' + c.slug, out, function () { location.reload(); }); return; }
    var b = ev.target.closest('button[data-arch]'); if (!b) return;
    api('PATCH', path, {archived: !c.archived})
      .then(function () { location.reload(); }).catch(function (e) { note(out, e.message); });
  });
  return h('details', null, h('summary', {text: 'Edit'}), f);
}
function newChannel(w) {
  var out = h('p');
  var f = h('form', null,
    h('div', {'class': 'two'}, field('Slug (becomes #' + w.slug + '.slug)', 'slug', 'text', '', {required: '', pattern: '[A-Za-z0-9][A-Za-z0-9._-]{1,63}'}),
      field('Name', 'name', 'text', '', {required: ''})),
    field('Description', 'description', 'textarea'),
    field('Focus: what a bot working a thread here should keep in mind', 'focus', 'textarea'),
    field('References into the knowledge base, one path per line; add "mandatory" to require it', 'references', 'textarea'),
    seedFields(), h('button', {type: 'submit', text: 'Create channel'}), out);
  f.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var body = {slug: f.elements.slug.value.trim(), name: f.elements.name.value.trim(), description: f.elements.description.value.trim(),
                focus: f.elements.focus.value.trim(), references: refsOf(f.elements.references.value)};
    var seed = seedOf(f); if (seed) body.seed = seed;
    api('POST', '/api/workspaces/' + encodeURIComponent(w.slug) + '/channels', body)
      .then(function () { location.reload(); }).catch(function (e) { note(out, e.message); });
  });
  return h('details', null, h('summary', {text: '+ New channel'}), f);
}
// ---- the knowledge base: a folder tree beside a reader --------------------------
// Markdown is rendered from escaped text, so nothing a bot writes becomes markup.
function esc(t) { return String(t).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
function inline(t) {
  return esc(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|[A-Za-z0-9_.\/-]+\.md)\)/g, function (m, label, href) {
      return /^https?:/.test(href) ? '<a href="' + href + '" target="_blank" rel="noopener">' + label + '</a>'
                                    : '<a href="#doc=' + href + '" data-doc="' + href + '">' + label + '</a>';
    })
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
}
function markdown(src) {
  var out = [], lines = String(src).replace(/\r\n/g, '\n').split('\n'), i = 0, list = null, para = [];
  function flush() {
    if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; }
    if (list) { out.push('</' + list + '>'); list = null; }
  }
  while (i < lines.length) {
    var l = lines[i], m;
    if (/^```/.test(l)) {
      flush(); var code = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
      out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>'); i++; continue;
    }
    if ((m = /^(#{1,4})\s+(.*)$/.exec(l))) { flush(); out.push('<h' + (m[1].length + 2) + '>' + inline(m[2]) + '</h' + (m[1].length + 2) + '>'); }
    else if ((m = /^\s*([-*]|\d+[.)])\s+(.*)$/.exec(l))) {
      var kind = /\d/.test(m[1]) ? 'ol' : 'ul';
      if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; }
      if (list !== kind) { if (list) out.push('</' + list + '>'); out.push('<' + kind + '>'); list = kind; }
      out.push('<li>' + inline(m[2]) + '</li>');
    }
    else if (/^\s*>\s?/.test(l)) { flush(); out.push('<blockquote>' + inline(l.replace(/^\s*>\s?/, '')) + '</blockquote>'); }
    else if (/^\s*(---|\*\*\*)\s*$/.test(l)) { flush(); out.push('<hr>'); }
    else if (!l.trim()) flush();
    else { if (list) { out.push('</' + list + '>'); list = null; } para.push(l); }
    i++;
  }
  flush();
  return out.join('');
}
var openDoc = function () {};    // set by knowledgeSection; channel references call it
function knowledgeSection(w) {
  var base = '/api/workspaces/' + encodeURIComponent(w.slug) + '/knowledge';
  var sec = h('section', {id: 'kb'}, h('h2', null, 'Knowledge base', h('small', {text: String(w.documents) + ' document' + (w.documents === 1 ? '' : 's')})));
  sec.appendChild(h('p', {'class': 'sub', text: "The workspace's shared memory: revisioned documents its bots read and write. INDEX.md is where a bot starts; a channel's folder holds what that channel worked out."}));
  var search = h('input', {type: 'search', placeholder: 'search the documents…', 'aria-label': 'Search the documents'});
  var tree = h('nav', {'class': 'kb-tree', 'aria-label': 'Documents'});
  var reader = h('article', {'class': 'kb-reader'}, h('p', {'class': 'sub', text: 'Pick a document.'}));
  sec.appendChild(h('div', {'class': 'kb'}, h('div', {'class': 'kb-side'}, search, tree), reader));
  var docs = [], current = null;
  var enc = function (p) { return p.split('/').map(encodeURIComponent).join('/'); };
  function drawTree(list, q) {
    tree.replaceChildren();
    if (!list.length) { tree.appendChild(h('p', {'class': 'sub', text: q ? 'No document matches.' : 'Nothing written yet.'})); return; }
    var folders = {}, order = [];
    list.forEach(function (d) {
      var k = d.path.indexOf('/') > 0 ? d.path.slice(0, d.path.lastIndexOf('/')) : '';
      if (!folders[k]) { folders[k] = []; order.push(k); }
      folders[k].push(d);
    });
    order.sort(function (a, b) { return a === '' ? -1 : b === '' ? 1 : a.localeCompare(b); });
    order.forEach(function (k) {
      var items = folders[k].sort(function (a, b) {
        var ai = /(^|\/)INDEX\.md$/.test(a.path), bi = /(^|\/)INDEX\.md$/.test(b.path);
        return ai !== bi ? (ai ? -1 : 1) : a.path.localeCompare(b.path);
      });
      var group = h('div', {'class': 'kb-folder'}, k ? h('div', {'class': 'kb-fname', text: k + '/'}) : null);
      items.forEach(function (d) {
        var b = h('button', {type: 'button', 'class': 'kb-item' + (d.path === current ? ' on' : ''), 'data-path': d.path,
                             title: d.path + ' · revision ' + d.revision + ' · ' + d.bytes + ' bytes'},
                  d.path.slice(k ? k.length + 1 : 0));
        b.addEventListener('click', function () { show(d.path); });
        group.appendChild(b);
      });
      tree.appendChild(group);
    });
  }
  function show(path, rev) {
    current = path;
    Array.prototype.forEach.call(tree.querySelectorAll('.kb-item'), function (b) { b.classList.toggle('on', b.dataset.path === path); });
    history.replaceState(null, '', '#doc=' + path + (rev ? '@' + rev : ''));
    reader.replaceChildren(h('p', {'class': 'sub', text: 'Loading ' + path + '…'}));
    var url = base + '/' + enc(path) + (rev ? '?revision=' + rev : '');
    Promise.all([api('GET', url), api('GET', base + '/' + enc(path) + '/history').catch(function () { return null; })]).then(function (rs) {
      var d = rs[0].document, revs = (rs[1] && (rs[1].revisions || rs[1].history)) || [];
      var meta = h('div', {'class': 'kb-meta'},
        h('span', {'class': 'kb-path', text: d.path}),
        h('span', {text: 'revision ' + d.revision + (rev ? ' (older)' : '')}),
        h('span', {text: (d.created_at || '').replace('T', ' ').slice(0, 16)}),
        d.author ? h('a', {href: '/a/' + encodeURIComponent(d.author), text: '@' + d.author}) : null);
      var body = h('div', {'class': 'md'}); body.innerHTML = markdown(d.body);
      var foot = h('div', {'class': 'kb-foot'});
      if (d.reason) foot.appendChild(h('p', {'class': 'sub', text: 'Why this revision: ' + d.reason}));
      if (d.sources && d.sources.length) foot.appendChild(h('p', {'class': 'sub'}, 'Sources: ', d.sources.map(function (s, i) {
        var link = /^\d+$/.test(String(s)) ? h('a', {href: '/t/' + s, text: 'message ' + s})
                 : /^https?:/.test(String(s)) ? h('a', {href: s, target: '_blank', rel: 'noopener', text: s}) : String(s);
        return [i ? ', ' : '', link];
      })));
      if (revs.length > 1) {
        var hl = h('div', {'class': 'kb-hist'}, h('span', {'class': 'sub', text: 'History: '}));
        revs.forEach(function (r) {
          var b = h('button', {type: 'button', 'class': 'link' + (r.revision === d.revision ? ' on' : ''),
                               title: (r.reason || '') + (r.author ? ' · @' + r.author : ''), text: 'r' + r.revision});
          b.addEventListener('click', function () { show(path, r.revision === revs[0].revision ? null : r.revision); });
          hl.appendChild(b);
        });
        foot.appendChild(hl);
      }
      reader.replaceChildren(meta, body, foot);
    }).catch(function (e) { reader.replaceChildren(h('p', {'class': 'msg-err', text: e.message})); });
  }
  openDoc = function (path) { sec.scrollIntoView({behavior: 'smooth'}); show(path); };
  reader.addEventListener('click', function (ev) {
    var a = ev.target.closest('a[data-doc]'); if (!a) return;
    ev.preventDefault();
    var target = a.dataset.doc, dir = current && current.indexOf('/') > 0 ? current.slice(0, current.lastIndexOf('/') + 1) : '';
    var resolve = function (p) {      // a link is relative to the document holding it
      var out = [];
      p.split('/').forEach(function (seg) { if (seg === '..') out.pop(); else if (seg && seg !== '.') out.push(seg); });
      return out.join('/');
    };
    var exists = function (p) { return docs.some(function (d) { return d.path === p; }); };
    var rel = resolve(dir + target);
    show(exists(rel) || !exists(resolve(target)) ? rel : resolve(target));
  });
  var timer = null;
  search.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      var q = search.value.trim();
      if (!q) { drawTree(docs, ''); return; }
      api('GET', base + '?q=' + encodeURIComponent(q)).then(function (d) { drawTree(d.documents, q); })
        .catch(function (e) { tree.replaceChildren(h('p', {'class': 'msg-err', text: e.message})); });
    }, 250);
  });
  window.addEventListener('hashchange', function () {     // a pasted or edited #doc= link
    var m = /#doc=([^@]+)(?:@(\d+))?$/.exec(decodeURIComponent(location.hash));
    if (m) show(m[1], m[2] ? Number(m[2]) : null);
  });
  api('GET', base).then(function (d) {
    docs = d.documents; drawTree(docs, '');
    var m = /#doc=([^@]+)(?:@(\d+))?$/.exec(decodeURIComponent(location.hash));
    if (m) show(m[1], m[2] ? Number(m[2]) : null);
    else if (docs.some(function (x) { return x.path === 'INDEX.md'; })) show('INDEX.md');
  }).catch(function (e) { tree.replaceChildren(h('p', {'class': 'msg-err', text: e.message})); });
  return sec;
}
function settingsSection(w) {
  var out = h('p');
  var f = h('form', null, field('Name', 'name', 'text', w.name),
    field('Purpose', 'purpose', 'textarea', w.purpose),
    h('div', {'class': 'row'}, h('button', {type: 'submit', text: 'Save'}), h('button', {type: 'button', 'class': 'quiet', text: 'Archive workspace', 'data-arch': '1'})), out);
  f.addEventListener('submit', function (ev) {
    ev.preventDefault();
    api('PATCH', '/api/workspaces/' + encodeURIComponent(w.slug), {name: f.elements.name.value.trim(), purpose: f.elements.purpose.value.trim()})
      .then(function () { location.reload(); }).catch(function (e) { note(out, e.message); });
  });
  f.addEventListener('click', function (ev) {
    var b = ev.target.closest('button[data-arch]'); if (!b) return;
    if (!confirm('Archive ' + w.slug + '? Its channels become read-only and its bots are released.')) return;
    api('PATCH', '/api/workspaces/' + encodeURIComponent(w.slug), {archived: true, archive_confirm: true})
      .then(function () { location.reload(); }).catch(function (e) { note(out, e.message); });
  });
  return h('section', null, h('h2', {text: 'Settings'}), f);
}

// Deleting (#473): only what is archived, after a warning that counts what goes, with the slug typed back.
function deleteFlow(path, slug, label, out, done) {
  api('GET', path + '/deletion').then(function (d) {
    var g = d.deletion, n = function (k, one) { return g[k] + ' ' + one + (g[k] === 1 ? '' : 's'); };
    if (g.bots && g.bots.length) {
      note(out, 'Move or delete its bots first: ' + g.bots.map(function (b) { return '@' + b; }).join(', ') + '. Change them under Bots, or on the fleet page.');
      return;
    }
    var what = [g.channels.length > 1 ? n('channels', 'channel') : null, n('threads', 'thread'), n('messages', 'message'),
                n('attachments', 'attachment'), g.documents !== undefined ? n('documents', 'knowledge document') : null]
      .filter(Boolean).join(', ');
    var typed = prompt('Delete ' + label + ' for good?\n\nThis removes ' + what + '.' +
      (g.routines.length ? ' Routines that post there are deleted too: ' + g.routines.join(', ') + '.' : '') +
      ' It cannot be undone.\n\nType ' + slug + ' to confirm.');
    if (typed === null) return;
    if (typed.trim() !== slug) { note(out, 'Not deleted: the slug you typed does not match.'); return; }
    api('DELETE', path, {confirm: slug}).then(done).catch(function (e) { note(out, e.message); });
  }).catch(function (e) { note(out, e.message); });
}
function deleteSection(w) {
  var out = h('p'), b = h('button', {type: 'button', 'class': 'quiet danger', text: 'Delete this workspace…'});
  b.addEventListener('click', function () {
    deleteFlow('/api/workspaces/' + encodeURIComponent(w.slug), w.slug, 'the workspace ' + w.slug, out,
               function () { location.href = '/workspaces'; });
  });
  return h('section', null, h('h2', {text: 'Delete'}),
    h('p', {'class': 'sub', text: 'An archived workspace can be deleted with everything in it: its channels, their messages and attachments, the routines that post there and its knowledge base. Its bots must be moved or deleted first.'}),
    b, out);
}

if (SLUG) wsView(); else listView();
})();
"""


def workspaces_page(name: str) -> str:
    """The whole page for /workspaces and /workspaces/{slug}; the script fills it in."""
    return shell(name, "workspaces", here="/workspaces", content=
                 '<style>' + WS_CSS + '</style>'
                 '<div class="ws" id="ws"><p class="sub">Loading workspaces…</p>'
                 '<noscript><p class="sub">This page needs JavaScript. The same data is at '
                 '<a href="/api/workspaces">/api/workspaces</a>.</p></noscript></div>'
                 '<script>' + WS_JS + '</script>')
