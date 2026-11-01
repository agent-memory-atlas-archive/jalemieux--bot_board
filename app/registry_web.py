"""Skills and profiles on /fleet, and their pages under /fleet/skills/ and /fleet/profiles/.

The fleet page gets two sections between Machines and Versions: Skills (every skill,
its revision and the profiles that list it; + Add a skill opens an inline panel with
one form, the skill written there) and Profiles (every profile, its skills and
the bots that carry it; view, clone, and delete only when no bot carries it). Each
skill has a page with an editor, history and who uses it; each profile a read-only
page with what it says, its fixed skills, its bots, the text a bot reads, and its
lineage. One form, /fleet/profiles/new, creates a profile from a template, a blank, or
an existing profile (which is what Clone is).

Writes go through the API with the identity the board page keeps in this browser
(localStorage bb.me). Data from the API only ever lands on the page as text.
"""
from __future__ import annotations

import json
import re

from . import registry
from .web import FLEET_CSS, ago, e, shell

STARS = "★"


def _json(data) -> str:
    """JSON for a <script type="application/json"> block: no '<' survives, so no text from the
    API can close the block or start a tag."""
    return json.dumps(data).replace("<", "\\u003c")


# ---------------------------------------------------------------- markdown

def md_html(text: str) -> str:
    """Enough Markdown for a SKILL.md: headings, fenced code, lists, paragraphs, `code`
    and **bold**. Everything is escaped first; the client-side preview does the same."""
    out, para, lst, code = [], [], None, None

    def flush_para():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def close_list():
        nonlocal lst
        if lst:
            out.append(f"</{lst}>")
            lst = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if code is not None:
            if line.startswith("```"):
                out.append("<pre><code>" + e("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.startswith("```"):
            flush_para(); close_list()
            code = []
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_para(); close_list()
            n = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            continue
        m = re.match(r"^\s*([-*]|\d+[.)])\s+(.*)$", line)
        if m:
            flush_para()
            kind = "ul" if m.group(1) in "-*" else "ol"
            if lst != kind:
                close_list()
                out.append(f"<{kind}>")
                lst = kind
            out.append("<li>" + _inline(m.group(2)) + "</li>")
            continue
        if not line.strip():
            flush_para(); close_list()
            continue
        if lst and raw.startswith(("  ", "\t")):
            out[-1] = out[-1][:-5] + " " + _inline(line.strip()) + "</li>"
            continue
        close_list()
        para.append(line.strip())
    flush_para(); close_list()
    if code is not None:
        out.append("<pre><code>" + e("\n".join(code)) + "</code></pre>")
    return "\n".join(out)


def _inline(s: str) -> str:
    s = e(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    return s


# ---------------------------------------------------------------- pieces

def _paragraphs(text: str) -> str:
    """Blank lines separate paragraphs; a single newline is a wrapped line."""
    return "".join(f"<p>{e(' '.join(p.split()))}</p>" for p in re.split(r"\n\s*\n", text.strip()) if p.strip())


def _revision(skill: dict) -> str:
    return f'rev {skill["revision"]} · {e(ago(skill["updated_at"]))} · {e(skill["author"])}'


def _used_by(skill: dict, bots_of: dict) -> str:
    names = skill.get("used_by") or []
    if not names:
        return '<span class="dim">no profile uses it</span>'
    n = sum(len(bots_of.get(p, [])) for p in names)
    chips = " ".join(f'<a class="chip" href="/fleet/profiles/{e(p)}">{e(p)}</a>' for p in names)
    return f'<div class="chips">{chips}</div><div class="by">{n} bot{"s" * (n != 1)}</div>'


def _skill_chip(s: dict) -> str:
    star = f' <span class="star" title="mandatory">{STARS}</span>' if s.get("mandatory") else ""
    return f'<a class="chip" href="/fleet/skills/{e(s["name"])}">{e(s["name"])}{star}</a>'


# ---------------------------------------------------------- fleet sections

ADD_SKILL_PANEL = """<details id="addskill" class="addbot-cta">
<summary><span class="addbot-btn">+ Add a skill</span></summary>
<form class="reg-form" id="skill-add" aria-label="Add a skill">
<label class="fld"><span>Name <small>· slug, this is how profiles name it</small></span>
<input name="name" required pattern="[a-z0-9][a-z0-9_\\-]{1,63}" placeholder="art-to-3d" autocomplete="off"></label>
<label class="fld"><span>Description <small>· one line; this is what a bot sees in its profile</small></span>
<input name="description" required maxlength="500" placeholder="Turn approved concept art into a game-ready 3D model." autocomplete="off"></label>
<label class="fld"><span>Instructions <small>· Markdown; the body of SKILL.md, or a whole SKILL.md pasted in. Name and description become its frontmatter.</small></span>
<textarea name="body" required rows="12" placeholder="## When to use this&#10;&#10;## Steps&#10;1. &#10;&#10;## What not to do"></textarea></label>
<div class="row"><button type="submit" class="addbot-go">Add skill</button>
<span class="note" id="skill-note">It appears in the list; a profile created or cloned from now on can tick it.</span></div>
<p class="status-line" role="status" aria-live="polite"></p>
</form>
</details>"""


def skills_section(skills: list, profiles: list) -> str:
    bots_of = {p["name"]: p.get("bots") or [] for p in profiles}
    rows = []
    for s in skills:
        page = f'/fleet/skills/{e(s["name"])}'
        acts = f'<a class="act" href="{page}">edit</a><a class="act" href="{page}#history">history</a>'
        rows.append(f'<tr id="skill-{e(s["name"])}"><td class="who"><a href="{page}">{e(s["name"])}</a>'
                    f'<div class="by desc">{e(s["description"])}</div></td>'
                    f'<td data-label="revision" class="nw">{_revision(s)}</td>'
                    f'<td data-label="used by">{_used_by(s, bots_of)}</td><td class="acts">{acts}</td></tr>')
    table = (('<table class="bots skills"><thead><tr><th>Skill</th><th>Revision</th><th>Used by</th>'
              '<th><span class="sr-only">Actions</span></th></tr></thead><tbody>' + "".join(rows) + '</tbody></table>')
             if rows else '<div class="empty">No skill yet. Add one above.</div>')
    return (f'<section class="fleet-section reg" id="fleet-skills"><div class="sec-head"><h2>Skills · {len(skills)}</h2>'
            f'{ADD_SKILL_PANEL}</div>'
            '<p class="sub">Fleet-wide, and the board is their only home. A bot gets the skills of its profile when its '
            'session starts; an edit reaches every bot on it at its next prompt, which tells it to read the skill again.</p>'
            f'{table}</section>')


def profiles_section(profiles: list, orphans: list) -> str:
    rows = []
    for p in profiles:
        page = f'/fleet/profiles/{e(p["name"])}'
        bots = p.get("bots") or []
        # the mandatory skills and one more name it; the page lists them all
        all_skills = p.get("skills") or []
        shown = [s for s in all_skills if s.get("mandatory")] or all_skills[:1]
        shown += [s for s in all_skills if s not in shown][:1]
        left = len(all_skills) - len(shown)
        chips = " ".join(_skill_chip(s) for s in shown) or '<span class="dim">none</span>'
        if left > 0:
            chips += f' <a class="dim" href="{page}">+ {left} more</a>'
        who = (f'{len(bots)} · ' + ", ".join(f'<a href="/a/{e(b)}">{e(b)}</a>' for b in bots)) if bots else '0 · no bot yet'
        origin = (f'<div class="by">cloned from <a href="/fleet/profiles/{e(p["from"])}">{e(p["from"])}</a></div>'
                  if p.get("from") and p["from"].lower() != p["name"].lower() else "")
        acts = (f'<a class="act" href="{page}">view</a><a class="act" href="/fleet/profiles/new?from={e(p["name"])}">clone</a>'
                + (f'<button type="button" class="act del-profile" data-name="{e(p["name"])}">delete</button>' if not bots else ""))
        spec = ' <span class="chip" title="bound to channels when added">specialized</span>' if p.get("specialized") else ""
        rows.append(f'<tr id="profile-{e(p["name"])}"><td class="who"><a href="{page}">{e(p["name"])}</a>{spec}'
                    f'<div class="by desc">{e(p["role"])}</div>{origin}</td>'
                    f'<td data-label="skills"><div class="chips">{chips}</div></td>'
                    f'<td data-label="bots">{who}</td><td class="acts">{acts}</td></tr>')
    if orphans:
        who = ", ".join(f'<a href="/a/{e(b)}">{e(b)}</a>' for b in orphans)
        rows.append(f'<tr id="profile-none"><td class="who"><span class="dim">no role</span>'
                    '<div class="by desc">Bots added before roles. To give one a role, retire it and add a new bot.</div></td>'
                    f'<td data-label="skills"><span class="dim">—</span></td><td data-label="bots">{len(orphans)} · {who}</td>'
                    '<td class="acts"></td></tr>')
    table = (('<table class="bots profiles"><thead><tr><th>Role</th><th>Skills</th><th>Bots</th>'
              '<th><span class="sr-only">Actions</span></th></tr></thead><tbody>' + "".join(rows) + '</tbody></table>')
             if rows else '<div class="empty">No profile yet.</div>')
    return (f'<section class="fleet-section reg" id="fleet-profiles"><div class="sec-head"><h2>Roles · {len(profiles)}</h2>'
            '<a class="addbot-btn needs-me" href="/fleet/profiles/new">+ Create a role</a></div>'
            '<p class="sub">A role is a profile: what a bot is for, and the skills that go with it. A bot is given one when you add it '
            'and keeps it. A role never changes: clone it to make a different one.</p>'
            f'{table}</section>')


REG_SECTION_JS = r"""
(function () {
  var ME = null; try { ME = JSON.parse(localStorage.getItem('bb.me')); } catch (err) {}
  function need() { if (!ME) alert('Sign in on the board page first (the "sign in to post" button), then come back.'); return !!ME; }
  function call(method, path, body) {
    return fetch(path, {method: method, headers: {'content-type': 'application/json', 'Authorization': 'Bearer ' + (ME ? ME.token : '')},
                        body: body ? JSON.stringify(body) : undefined})
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error(d.error || d.detail || ('refused: ' + r.status)); return d; }); });
  }
  var form = document.getElementById('skill-add');
  if (form) {
    var msg = form.querySelector('.status-line');
    form.addEventListener('submit', function (ev) {
      ev.preventDefault(); if (!need()) return;
      var name = form.elements.name.value.trim().toLowerCase();
      msg.textContent = 'Saving…';
      call('PUT', '/api/skills/' + encodeURIComponent(name),
           {body: form.elements.body.value, fields: {description: form.elements.description.value.trim()},
            base_revision: 0, reason: 'added on the fleet page'})
      .then(function () { location.href = '/fleet/skills/' + encodeURIComponent(name); },
            function (e) { msg.textContent = e.message; });
    });
  }
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('button.del-profile'); if (!b || !need()) return;
    if (!confirm('Delete the profile ' + b.dataset.name + '? No bot carries it. The name is never reused.')) return;
    call('DELETE', '/api/profiles/' + encodeURIComponent(b.dataset.name))
      .then(function () { location.reload(); }, function (e) { alert(e.message); });
  });
})();
"""

REG_CSS = """
.fleet-page .reg .sec-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}
.fleet-page .reg .sec-head h2{margin-top:6px}
.fleet-page .reg .desc{color:var(--dim);font-family:var(--sans);white-space:normal;max-width:46em}
.fleet-page .reg td.who{white-space:normal}
.fleet-page .reg .chip{text-decoration:none;color:var(--ink)}
.fleet-page .reg .chip .star{color:var(--accent)}
.fleet-page .reg .acts a.act{text-decoration:none;display:inline-block}
.fleet-page .reg a.addbot-btn{text-decoration:none}
body.out .fleet-page .needs-me,body.out .fleet-page details#addskill{display:none}
.fleet-page details#addskill>summary{list-style:none;cursor:pointer}
.fleet-page details#addskill>summary::-webkit-details-marker{display:none}
.fleet-page details#addskill[open]{flex-basis:100%;border:1px solid var(--line);border-radius:10px;background:var(--panel);margin:0 0 12px;padding:10px 0 0}
.fleet-page details#addskill[open]>summary{padding:0 16px 8px}
.fleet-page .tabs{display:flex;gap:4px;padding:0 16px;border-bottom:1px solid var(--line)}
.fleet-page .tabs .tab{background:none;border:0;border-bottom:2px solid transparent;padding:6px 10px;font:600 13px var(--sans);color:var(--dim);cursor:pointer}
.fleet-page .tabs .tab[aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent)}
.fleet-page .reg-form{display:flex;flex-direction:column;gap:12px;padding:12px 16px 14px}
.fleet-page .reg-form .fld{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--dim)}
.fleet-page .reg-form .fld small{color:var(--dim);font-weight:400}
.fleet-page .reg-form input,.fleet-page .reg-form textarea,.fleet-page .reg-form select{background:var(--bg);border:1px solid var(--line);
 color:var(--ink);border-radius:6px;padding:6px 8px;font:13px/1.45 var(--mono);width:100%;box-sizing:border-box}
.fleet-page .reg-form textarea{resize:vertical;white-space:pre-wrap}
.fleet-page .reg-form .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.fleet-page .reg-form .note{font-size:12px;color:var(--dim)}
.fleet-page .reg-form .status-line{margin:0;font-size:13px;color:var(--accent);min-height:1em}
.fleet-page .reg-form .status-line.ok{color:var(--good)}
.fleet-page .reg-form button:disabled{opacity:.5;cursor:default}
.fleet-page .crumbs{font-size:13px;color:var(--dim);margin:0 0 10px}.fleet-page .crumbs a{color:var(--dim)}
.fleet-page .reg-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:0 0 6px}
.fleet-page .reg-head h1{margin:0;font-family:var(--mono)}
.fleet-page p.role{margin:0 0 6px;font-size:15px}
.fleet-page .reg-head .right{margin-left:auto;display:flex;gap:8px;align-items:center;font-size:13px;color:var(--dim)}
.fleet-page .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0 0 18px}
.fleet-page .card h2{margin:0 0 10px}
.fleet-page .card dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:0;font-size:13.5px}
.fleet-page .card dt{color:var(--dim);font:12px var(--mono)}.fleet-page .card dd{margin:0;max-width:60em}
.fleet-page .card dd p{margin:0 0 6px}.fleet-page .card dd p:last-child{margin-bottom:0}
.fleet-page .card ul.hist{list-style:none;margin:0;padding:0}
.fleet-page .card ul.hist li{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;padding:6px 0;border-top:1px solid var(--line);font-size:13px}
.fleet-page .card table.bots{table-layout:auto}.fleet-page .card{min-width:0;overflow:hidden}
.fleet-page .card ul.hist li:first-child{border-top:0}
.fleet-page .card ul.hist .rev{font-family:var(--mono);white-space:nowrap}
.fleet-page .card ul.hist .reason{flex:1;min-width:12em}
.fleet-page .card.danger{border-color:var(--accent)}
.fleet-page .card.danger p{margin:4px 0 0;font-size:13px;color:var(--dim)}
.fleet-page pre.what{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;white-space:pre-wrap;font-size:12.5px;max-height:28em;overflow:auto}
.fleet-page .md h3,.fleet-page .md h4,.fleet-page .md h5{margin:14px 0 6px}.fleet-page .md p{margin:6px 0}
.fleet-page .md pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;overflow:auto}
.fleet-page .two{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:18px}
@media(max-width:900px){.fleet-page .two{grid-template-columns:1fr}}
.fleet-page .starters{display:flex;flex-wrap:wrap;gap:6px}
.fleet-page .starters button{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:999px;padding:4px 12px;font:13px var(--sans);cursor:pointer}
.fleet-page .starters button[aria-pressed=true]{border-color:var(--accent);background:var(--accent-soft);color:var(--accent);font-weight:600}
.fleet-page table.bots.pick td{vertical-align:middle}
.fleet-page table.bots.pick input[type=checkbox]{width:auto}
"""


# -------------------------------------------------------------- skill page

def skill_page(board_name: str, skill: dict, history: list, profiles: list) -> str:
    name = e(skill["name"])
    bots_of = {p["name"]: p.get("bots") or [] for p in profiles}
    used = skill.get("used_by") or []
    used_html = "".join(
        f'<li><a class="chip" href="/fleet/profiles/{e(p)}">{e(p)}</a> <span class="dim">{len(bots_of.get(p, []))} bot'
        f'{"s" * (len(bots_of.get(p, [])) != 1)}' + (" · " + ", ".join(f"@{e(b)}" for b in bots_of.get(p, [])) if bots_of.get(p) else "")
        + '</span></li>' for p in used) or '<li class="dim">No profile lists it yet. A profile created or cloned from now on can.</li>'
    hist_html = "".join(
        f'<li><span class="rev">rev {h["revision"]}</span><span class="dim nw">{e(ago(h["updated_at"]))} · {e(h["author"])}</span>'
        f'<span class="reason">{e(h["reason"]) or "<span class=dim>no reason given</span>"}</span>'
        + (f'<button type="button" class="act restore needs-me" data-rev="{h["revision"]}">restore</button>'
           if h["revision"] != skill["revision"] and not h["deleted"] else "")
        + ("<span class=dim>deleted</span>" if h["deleted"] else "") + '</li>' for h in history)
    data = _json({"name": skill["name"], "revision": skill["revision"], "description": skill["description"],
                  "body": skill["body"], "bots": sorted({b for p in used for b in bots_of.get(p, [])})})
    danger = (f'<button type="button" class="act del-skill" data-name="{name}" data-rev="{skill["revision"]}"'
              + (" disabled" if used else "") + '>Delete this skill</button>'
              + (f'<p>Refused while {len(used)} profile{"s" * (len(used) != 1)} list{"" if len(used) != 1 else "s"} it: '
                 f'{", ".join(e(p) for p in used)}. Clone {"them" if len(used) != 1 else "it"} without it, delete the old, then come back.</p>'
                 if used else '<p>No profile lists it. History is kept; the name can be used again.</p>'))
    body_field = f'<textarea name="body" rows="22" required>{e(skill["body"])}</textarea>'
    content = f"""
<style>{FLEET_CSS}{REG_CSS}</style>
<div class="fleet-page"><div class="reg">
<p class="crumbs"><a href="/fleet">Fleet</a> › <a href="/fleet#fleet-skills">Skills</a> › {name}</p>
<div class="reg-head"><h1>{name}</h1><div class="right">{_revision(skill)}</div></div>
<p class="sub">{e(skill["description"])}</p>
<div class="two"><div>
<div class="card" id="editor">
<div class="tabs" role="tablist" style="padding:0;margin:0 0 10px">
<button type="button" class="tab" role="tab" aria-selected="true" data-tab="edit">Edit</button>
<button type="button" class="tab" role="tab" aria-selected="false" data-tab="preview">Preview</button>
<button type="button" class="tab" role="tab" aria-selected="false" data-tab="gets">What a bot gets</button>
</div>
<form class="reg-form" id="skill-edit" style="padding:0">
<label class="fld"><span>Name</span><input name="name" value="{name}" readonly></label>
<label class="fld"><span>Description <small>· one line; this is what a bot sees in its profile</small></span>
<input name="description" value="{e(skill["description"])}" maxlength="500" required></label>
<label class="fld"><span>Instructions <small>· Markdown; the body of SKILL.md</small></span>{body_field}</label>
<label class="fld"><span>Reason for this change <small>· required, as in a knowledge write; it shows in History</small></span>
<input name="reason" required maxlength="500" placeholder="what changed, and why" autocomplete="off"></label>
<div class="row needs-me"><button type="submit" class="addbot-go">Save as rev {skill["revision"] + 1}</button>
<span class="note">Bots on it are told at their next prompt.</span></div>
<p class="status-line" role="status" aria-live="polite"></p>
</form>
<div class="md" id="skill-preview" hidden>{md_html(skill["body"])}</div>
<div id="skill-gets" hidden><p class="sub">Exactly what <code>GET /api/skills/{name}</code> returns, and the line a profile carries for it.</p>
<pre class="what" id="skill-text">{e(registry.skill_text(skill))}</pre>
<pre class="what">- **{name}**: {e(skill["description"])}</pre></div>
</div>
</div><div>
<div class="card"><h2>Details</h2><dl><dt>read at</dt><dd><code>GET /api/skills/{name}</code></dd>
<dt>revision</dt><dd>{skill["revision"]} · {e(ago(skill["updated_at"]))} · {e(skill["author"])}</dd></dl></div>
<div class="card"><h2>Used by · {len(used)}</h2><ul class="hist">{used_html}</ul>
<p class="sub" style="margin:8px 0 0">Read-only here: a profile's skill list is fixed when the profile is created.</p>
{'<button type="button" class="act needs-me" id="reset-all" style="margin-top:8px">New session for all bots on it</button>' if any(bots_of.get(p) for p in used) else ''}
<p class="status-line" id="reset-note" role="status" aria-live="polite"></p></div>
<div class="card" id="history"><h2>History · {len(history)}</h2><ul class="hist">{hist_html}</ul>
<p class="sub" style="margin:8px 0 0">Restore writes a new revision with the old body; nothing is rewritten in place.</p></div>
<div class="card danger"><h2>Danger</h2>{danger}</div>
</div></div>
<script type="application/json" id="skill-data">{data}</script>
</div></div>
<script>{SKILL_PAGE_JS}</script>"""
    return shell(board_name, f"skill {skill['name']}", content, here="/fleet")


SKILL_PAGE_JS = r"""
(function () {
  var ME = null; try { ME = JSON.parse(localStorage.getItem('bb.me')); } catch (err) {}
  if (!ME) document.body.classList.add('out');
  function need() { if (!ME) alert('Sign in on the board page first (the "sign in to post" button), then come back.'); return !!ME; }
  function call(method, path, body) {
    return fetch(path, {method: method, headers: {'content-type': 'application/json', 'Authorization': 'Bearer ' + (ME ? ME.token : '')},
                        body: body ? JSON.stringify(body) : undefined})
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error(d.error || d.detail || ('refused: ' + r.status)); return d; }); });
  }
  var S = JSON.parse(document.getElementById('skill-data').textContent);
  function resetAll(path, bots, note) {
    if (!need()) return;
    if (!confirm('Give ' + bots.map(function (h) { return '@' + h; }).join(', ') + ' a new session? Each reads its profile and skills again (when idle; a stopped bot at its next start).')) return;
    call('POST', path).then(function (d) {
      var n = Object.keys(d.skipped);
      note.className = 'status-line ok';
      note.textContent = d.reset.length + ' asked' + (n.length ? '; skipped: ' + n.map(function (h) { return '@' + h + ' (' + d.skipped[h] + ')'; }).join(', ') : '') + '.';
    }, function (e) { note.className = 'status-line'; note.textContent = e.message; });
  }
  var all = document.getElementById('reset-all');
  if (all) all.addEventListener('click', function () {
    resetAll('/api/skills/' + encodeURIComponent(S.name) + '/reset', S.bots, document.getElementById('reset-note'));
  });
  var form = document.getElementById('skill-edit');
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; }); }
  function inline(s) { return esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>'); }
  // the same Markdown subset the board renders: headings, fenced code, lists, paragraphs
  function md(text) {
    var out = [], para = [], list = null, code = null;
    function flush() { if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = []; } }
    function close() { if (list) { out.push('</' + list + '>'); list = null; } }
    text.split('\n').forEach(function (raw) {
      var line = raw.replace(/\s+$/, ''), m;
      if (code !== null) { if (line.indexOf('```') === 0) { out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>'); code = null; } else code.push(line); return; }
      if (line.indexOf('```') === 0) { flush(); close(); code = []; return; }
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { flush(); close(); var n = Math.min(m[1].length + 2, 6); out.push('<h' + n + '>' + inline(m[2]) + '</h' + n + '>'); return; }
      if ((m = line.match(/^\s*([-*]|\d+[.)])\s+(.*)$/))) { flush(); var k = /^[-*]$/.test(m[1]) ? 'ul' : 'ol'; if (list !== k) { close(); out.push('<' + k + '>'); list = k; } out.push('<li>' + inline(m[2]) + '</li>'); return; }
      if (!line.trim()) { flush(); close(); return; }
      if (list && /^(\s{2,}|\t)/.test(raw)) { out[out.length - 1] = out[out.length - 1].slice(0, -5) + ' ' + inline(line.trim()) + '</li>'; return; }
      close(); para.push(line.trim());
    });
    flush(); close(); if (code !== null) out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
    return out.join('\n');
  }
  function text() {
    var d = form.elements.description.value.trim();
    var q = /[:#"'\n]/.test(d) || !d ? JSON.stringify(d) : d;
    return '---\nname: ' + S.name + '\ndescription: ' + q + '\n---\n\n' + form.elements.body.value.trim() + '\n';
  }
  document.querySelectorAll('#editor button.tab').forEach(function (t) {
    t.addEventListener('click', function () {
      document.querySelectorAll('#editor button.tab').forEach(function (b) { b.setAttribute('aria-selected', String(b === t)); });
      form.hidden = t.dataset.tab !== 'edit';
      var p = document.getElementById('skill-preview'), g = document.getElementById('skill-gets');
      p.hidden = t.dataset.tab !== 'preview'; g.hidden = t.dataset.tab !== 'gets';
      if (!p.hidden) p.innerHTML = md(form.elements.body.value);
      if (!g.hidden) { document.getElementById('skill-text').textContent = text(); }
    });
  });
  form.addEventListener('submit', function (ev) {
    ev.preventDefault(); if (!need()) return;
    var msg = form.querySelector('.status-line'); msg.className = 'status-line'; msg.textContent = 'Saving…';
    call('PUT', '/api/skills/' + encodeURIComponent(S.name),
         {body: form.elements.body.value, fields: {description: form.elements.description.value.trim()},
          base_revision: S.revision, reason: form.elements.reason.value.trim()})
    .then(function (d) {
      msg.className = 'status-line ok';
      msg.textContent = 'Saved as rev ' + d.skill.revision + '. ' + (S.bots.length ? S.bots.length + ' bot' + (S.bots.length === 1 ? '' : 's') + ' on it read it again at their next prompt.' : 'No bot has a profile that lists it yet.');
      setTimeout(function () { location.reload(); }, 1200);
    }, function (e) { msg.textContent = e.message; });
  });
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('button.restore'); if (!b || !need()) return;
    var rev = b.dataset.rev;
    if (!confirm('Restore rev ' + rev + ' as a new revision ' + (S.revision + 1) + '? Nothing is rewritten; the revisions between stay in the history.')) return;
    fetch('/api/skills/' + encodeURIComponent(S.name) + '?json&revision=' + rev, {headers: {'Authorization': 'Bearer ' + ME.token}})
      .then(function (r) { return r.json(); }).then(function (d) {
        return call('PUT', '/api/skills/' + encodeURIComponent(S.name),
                    {body: d.skill.body, fields: {description: d.skill.description}, base_revision: S.revision, reason: 'restored rev ' + rev});
      }).then(function () { location.reload(); }, function (e) { alert(e.message); });
  });
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('button.del-skill'); if (!b || !need()) return;
    if (!confirm('Delete the skill ' + S.name + '? Its history is kept.')) return;
    call('DELETE', '/api/skills/' + encodeURIComponent(S.name), {base_revision: S.revision, reason: 'deleted on the fleet page'})
      .then(function () { location.href = '/fleet#fleet-skills'; }, function (e) { alert(e.message); });
  });
})();
"""


# ------------------------------------------------------------ profile page

def profile_page(board_name: str, profile: dict, bots: list, base: str) -> str:
    name = e(profile["name"])
    skills = registry.resolve(profile)
    skill_rows = "".join(
        f'<tr><td class="who"><a href="/fleet/skills/{e(s["name"])}">{e(s["name"])}</a>'
        + (f' <span class="star" title="mandatory">{STARS} mandatory</span>' if s["mandatory"] else "")
        + f'<div class="by desc">{e(s["description"])}</div></td>'
        f'<td data-label="revision" class="nw dim">rev {s["revision"]}</td></tr>'
        for s in skills)
    skills_html = (f'<table class="bots"><thead><tr><th>Skill</th><th>Rev</th></tr></thead><tbody>{skill_rows}</tbody></table>'
                   if skills else '<p class="sub">This profile brings no skills; its bots have the guide and their workspace\'s knowledge base.</p>')
    bot_rows = ""
    for b in bots:
        st = b.get("state") or {}
        last = b.get("last_run") or ""
        ws = f'<a href="/workspaces/{e(b["workspace"])}">{e(b["workspace"])}</a>' if b.get("workspace") else "—"
        tone = {"working": "run", "waiting": "ok", "stopped": "", "unknown": ""}.get(st.get("state"), "warn")
        bot_rows += (f'<tr><td class="who"><a href="/a/{e(b["handle"])}">@{e(b["handle"])}</a></td>'
                     f'<td data-label="workspace">{ws}</td>'
                     f'<td data-label="status"><span class="pip {tone}"></span>{e(st.get("words") or b.get("status") or "")}</td>'
                     f'<td data-label="last run" class="dim">{e(ago(last)) if last else "never"}'
                     f'<div class="by">on <a href="/fleet#box-{e(b["host"])}">{e(b["host"])}</a></div></td>'
                     f'<td class="acts"><button type="button" class="reset" data-handle="{e(b["handle"])}" '
                     'title="set its harness session aside; it reads the profile and its skills again">new session</button></td></tr>')
    bots_html = (f'<table class="bots"><thead><tr><th>Bot</th><th>Workspace</th><th>Status</th><th>Last run</th>'
                 f'<th><span class="sr-only">Actions</span></th></tr></thead><tbody>{bot_rows}</tbody></table>'
                 if bots else '<p class="sub">No bot carries it yet. Pick it under Profile when you add a bot.</p>')
    origin = profile.get("from") or ""
    templates = {t["name"] for t in registry.templates()}
    try:
        source = registry.get_profile(origin) if origin else None
    except KeyError:
        source = None
    if origin and origin.lower() == profile["name"].lower():
        from_html = f"from the shipped {e(origin)} template"
    elif source:
        from_html = f'cloned from <a href="/fleet/profiles/{e(origin)}">{e(origin)}</a>'
    elif origin in templates:
        from_html = f"from the {e(origin)} template"
    elif origin:
        from_html = f"from {e(origin)}, since deleted"
    else:
        from_html = "from a blank form"
    clones = profile.get("clones") or []
    clones_html = ("".join(f'<li><a class="chip" href="/fleet/profiles/{e(c)}">{e(c)}</a></li>' for c in clones)
                   if clones else '<li class="dim">no profile was cloned from it yet</li>')
    n = len(bots)
    danger = (f'<button type="button" class="act del-profile" data-name="{name}"{" disabled" if n else ""}>Delete this profile</button>'
              + (f'<p>Not while {n} bot{"s" * (n != 1)} carr{"y" if n != 1 else "ies"} it. Retire {"them" if n != 1 else "it"} first; the name is never reused.</p>'
                 if n else '<p>No bot carries it. The name stays taken: profiles are never reused under an old name.</p>'))
    says = "".join(f'<dt>{k}</dt><dd>{_paragraphs(profile.get(k) or "") or "<span class=dim>—</span>"}</dd>'
                   for k in ("responsibilities", "focus", "avoid"))
    content = f"""
<style>{FLEET_CSS}{REG_CSS}</style>
<div class="fleet-page"><div class="reg">
<p class="crumbs"><a href="/fleet">Fleet</a> › <a href="/fleet#fleet-profiles">Profiles</a> › {name}</p>
<div class="reg-head"><h1>{name}</h1>
<div class="right"><a class="addbot-btn needs-me" href="/fleet/profiles/new?from={name}">Clone</a></div></div>
<p class="role">{e(profile["role"])}</p>
<p class="sub">created {e(ago(profile["created_at"]))} by {e(profile["created_by"])} · {from_html}. A profile never changes: to change this one, clone it and add new bots with the clone.{" <b>Specialized:</b> a bot added with it is bound to channels of its workspace and claims new threads only there." if profile.get("specialized") else ""}</p>
<div class="two"><div>
<div class="card" id="says"><h2>What it says</h2><dl>{says}</dl></div>
<div class="card" id="skills"><h2>Skills · {len(skills)}</h2>{skills_html}
<p class="sub" style="margin:8px 0 0">This list does not change. Skill text does: a skill's edits reach these bots at their next prompt.</p></div>
<div class="card" id="bots"><h2>Bots with this profile · {n}</h2>
<p class="sub">A bot reads this profile and its skills when its session starts. New session makes one read them again.</p>{bots_html}
{'<button type="button" class="act needs-me" id="reset-all" style="margin-top:8px">New session for all ' + str(n) + ' bot' + 's' * (n != 1) + '</button><p class="status-line" id="reset-note" role="status" aria-live="polite"></p>' if n else ''}</div>
</div><div>
<div class="card" id="gets"><h2>What a bot gets</h2><p class="sub">The text <code>GET /api/me/profile</code> returns to a bot of this profile.</p>
<pre class="what">{e(registry.profile_text(profile, base))}</pre></div>
<div class="card" id="lineage"><h2>Lineage</h2><dl><dt>from</dt><dd>{from_html}</dd><dt>clones</dt><dd><ul class="hist" style="margin:0">{clones_html}</ul></dd></dl></div>
<div class="card danger"><h2>Danger</h2>{danger}</div>
</div></div>
</div></div>
<script>{PROFILE_PAGE_JS}</script>"""
    return shell(board_name, f"profile {profile['name']}", content, here="/fleet")


PROFILE_PAGE_JS = r"""
(function () {
  var ME = null; try { ME = JSON.parse(localStorage.getItem('bb.me')); } catch (err) {}
  if (!ME) document.body.classList.add('out');
  function need() { if (!ME) alert('Sign in on the board page first (the "sign in to post" button), then come back.'); return !!ME; }
  function call(method, path, body) {
    return fetch(path, {method: method, headers: {'content-type': 'application/json', 'Authorization': 'Bearer ' + (ME ? ME.token : '')},
                        body: body ? JSON.stringify(body) : undefined})
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error(d.error || d.detail || ('refused: ' + r.status)); return d; }); });
  }
  var all = document.getElementById('reset-all');
  if (all) all.addEventListener('click', function () {
    var bots = Array.prototype.map.call(document.querySelectorAll('button.reset'), function (b) { return b.dataset.handle; }), note = document.getElementById('reset-note');
    if (!need() || !confirm('Give ' + bots.map(function (h) { return '@' + h; }).join(', ') + ' a new session? Each reads its profile and skills again (when idle; a stopped bot at its next start).')) return;
    call('POST', '/api/profiles/' + encodeURIComponent(location.pathname.split('/').pop()) + '/reset').then(function (d) {
      var n = Object.keys(d.skipped);
      note.className = 'status-line ok';
      note.textContent = d.reset.length + ' asked' + (n.length ? '; skipped: ' + n.map(function (h) { return '@' + h + ' (' + d.skipped[h] + ')'; }).join(', ') : '') + '.';
    }, function (e) { note.className = 'status-line'; note.textContent = e.message; });
  });
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('button.reset'); if (!b || !need()) return;
    if (!confirm('Give @' + b.dataset.handle + ' a new session? It reads its profile and skills again and starts fresh (when idle; a stopped bot at its next start).')) return;
    call('POST', '/api/fleet/bots/' + encodeURIComponent(b.dataset.handle) + '/reset')
      .then(function () { b.textContent = 'asked'; b.disabled = true; }, function (e) { alert(e.message); });
  });
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('button.del-profile'); if (!b || !need()) return;
    if (!confirm('Delete the profile ' + b.dataset.name + '? The name is never reused.')) return;
    call('DELETE', '/api/profiles/' + encodeURIComponent(b.dataset.name))
      .then(function () { location.href = '/fleet#fleet-profiles'; }, function (e) { alert(e.message); });
  });
})();
"""


# ------------------------------------------------------------- create form

def profile_new_page(board_name: str, templates: list, profiles: list, skills: list, start: str | None) -> str:
    starters = ([{"key": "", "label": "Blank", "kind": "blank", "data": {"name": "", "role": "", "responsibilities": "",
                                                                          "focus": "", "avoid": "", "skills": [], "specialized": False}}]
                + [{"key": "template:" + t["name"], "label": t["name"], "kind": "template", "data": t} for t in templates]
                + [{"key": p["name"], "label": p["name"], "kind": "profile",
                    "data": {k: p.get(k) for k in ("name", "role", "responsibilities", "focus", "avoid", "skills", "specialized")}}
                   for p in profiles])
    keys = [s["key"] for s in starters]
    # ?from= names a profile (Clone) or a template; the default is the developer starter
    chosen = next((k for k in (start, "template:" + (start or ""), "developer", "template:developer") if k in keys), "")
    chips = "".join(f'<button type="button" data-key="{e(s["key"])}" aria-pressed="{"true" if s["key"] == chosen else "false"}">'
                    f'{e(s["label"])}{" <small>template</small>" if s["kind"] == "template" else ""}</button>'
                    for s in starters)
    skill_rows = "".join(
        f'<tr><td><input type="checkbox" name="skill" value="{e(s["name"])}" aria-label="include {e(s["name"])}"></td>'
        f'<td class="who"><a href="/fleet/skills/{e(s["name"])}">{e(s["name"])}</a><div class="by desc">{e(s["description"])}</div></td>'
        f'<td data-label="mandatory"><label><input type="checkbox" name="mandatory" value="{e(s["name"])}" aria-label="{e(s["name"])} mandatory"> read before any work</label></td></tr>'
        for s in skills)
    skills_html = (f'<table class="bots pick"><thead><tr><th><span class="sr-only">Include</span></th><th>Skill</th><th>Mandatory</th></tr></thead>'
                   f'<tbody>{skill_rows}</tbody></table>' if skills else
                   '<p class="sub">The registry is empty. A profile can be created without skills and more cannot be added later: add skills first if this profile needs them.</p>')
    taken = sorted(registry.taken_names(), key=str.lower)     # deleted ones too: a name is never reused
    data = _json({"starters": starters, "chosen": chosen, "taken": taken, "base": ""})
    content = f"""
<style>{FLEET_CSS}{REG_CSS}</style>
<div class="fleet-page"><div class="reg">
<p class="crumbs"><a href="/fleet">Fleet</a> › <a href="/fleet#fleet-profiles">Profiles</a> › new profile</p>
<div class="reg-head"><h1 style="font-family:var(--sans)">Create a profile</h1></div>
<p class="sub">A profile is a role and the skills that go with it. It cannot be changed once created: a bot keeps it for life, and a different role is a new profile.</p>
<div class="two"><div>
<div class="card"><h2>Start from</h2><div class="starters" id="starters">{chips}</div>
<p class="sub" style="margin:8px 0 0">Three templates ship with the board. Any existing profile can be picked too, which is what Clone does. Switching replaces the fields below; nothing is saved until Create.</p></div>
<form class="card reg-form" id="profile-new" style="gap:12px">
<label class="fld"><span>Name <small>· slug; cannot be changed later, and a name once used is never reused</small></span>
<input name="name" required pattern="[a-z0-9][a-z0-9_\\-]{{1,63}}" placeholder="developer-2" autocomplete="off"></label>
<label class="fld"><span>Role <small>· one line: what this bot is</small></span><input name="role" required maxlength="4000" autocomplete="off"></label>
<label class="fld"><span>Responsibilities <small>· what the bot is for, in a paragraph it can judge work against</small></span><textarea name="responsibilities" rows="4" maxlength="4000"></textarea></label>
<label class="fld"><span>Focus <small>· what to prefer when several threads are open</small></span><textarea name="focus" rows="4" maxlength="4000"></textarea></label>
<label class="fld"><span>Avoid <small>· work this bot leaves to others</small></span><textarea name="avoid" rows="3" maxlength="4000"></textarea></label>
<label class="fld"><span><input type="checkbox" name="specialized"> Specialized <small>· one kind of work (an artist, a lore writer): a bot added with it is bound to channels of its workspace and claims new threads only there. Leave it off for a profile that drives anything, such as a developer.</small></span></label>
<div><h2 style="margin-bottom:6px">Skills</h2>{skills_html}</div>
<div class="row needs-me"><button type="submit" class="addbot-go" id="create-go">Create</button><span class="note" id="create-note"></span></div>
<p class="status-line" role="status" aria-live="polite"></p>
</form>
</div><div>
<div class="card"><h2>What a bot will get</h2><pre class="what" id="preview"></pre></div>
<div class="card" id="starting"><h2>Starting point</h2><p class="sub" id="starting-text" style="margin:0"></p></div>
</div></div>
<script type="application/json" id="new-data">{data}</script>
</div></div>
<script>{NEW_PROFILE_JS}</script>"""
    return shell(board_name, "new profile", content, here="/fleet")


NEW_PROFILE_JS = r"""
(function () {
  var ME = null; try { ME = JSON.parse(localStorage.getItem('bb.me')); } catch (err) {}
  if (!ME) document.body.classList.add('out');
  var D = JSON.parse(document.getElementById('new-data').textContent);
  var form = document.getElementById('profile-new'), current = null;
  function starter(key) { return D.starters.filter(function (s) { return s.key === key; })[0]; }
  function fill(key) {
    var s = starter(key); if (!s) return; current = s;
    document.querySelectorAll('#starters button').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.key === key)); });
    form.elements.name.value = s.kind === 'blank' ? '' : (D.taken.indexOf(s.data.name) >= 0 ? s.data.name + '-2' : s.data.name);
    ['role', 'responsibilities', 'focus', 'avoid'].forEach(function (k) { form.elements[k].value = s.data[k] || ''; });
    form.elements.specialized.checked = !!s.data.specialized;
    var picked = {}; (s.data.skills || []).forEach(function (x) { picked[x.name] = !!x.mandatory; });
    form.querySelectorAll('input[name=skill]').forEach(function (c) { c.checked = c.value in picked; });
    form.querySelectorAll('input[name=mandatory]').forEach(function (c) { c.checked = !!picked[c.value]; });
    var st = document.getElementById('starting-text');
    st.textContent = s.kind === 'blank' ? 'A blank form. Nothing is recorded as lineage.'
      : s.kind === 'template' ? 'The ' + s.label + ' template that ships with the board. The profile records the template\'s name as what it started from.'
      : 'Everything above started as a copy of ' + s.label + '. The link is kept as lineage on both pages; ' + s.label + ' keeps its bots.';
    render();
  }
  function skills() {
    var mand = {}; form.querySelectorAll('input[name=mandatory]:checked').forEach(function (c) { mand[c.value] = true; });
    return Array.prototype.map.call(form.querySelectorAll('input[name=skill]:checked'), function (c) { return {name: c.value, mandatory: !!mand[c.value]}; });
  }
  function render() {
    var f = form.elements, name = f.name.value.trim() || '<name>';
    var lines = ['# Your profile: ' + name, '', '**Role.** ' + f.role.value.trim(), ''];
    if (f.specialized.checked) lines.push('**Specialized.** This profile is for one kind of work. The board offers you new threads only in the channels your bot was added for; elsewhere you take part when mentioned.', '');
    [['responsibilities', 'Responsibilities'], ['focus', 'Focus'], ['avoid', 'Avoid']].forEach(function (p) {
      if (f[p[0]].value.trim()) lines.push('## ' + p[1], '', f[p[0]].value.trim(), '');
    });
    lines.push('## Skills', '');
    var sk = skills();
    if (!sk.length) lines.push('This profile brings no skills; the guide and your workspace\'s knowledge base are what you have.');
    else { lines.push('Each skill is a SKILL.md you read with `GET /api/skills/<name>`. Read the mandatory ones now, before any work; the others when a thread calls for what they describe.', '');
      sk.forEach(function (s) { lines.push('- **' + s.name + '**' + (s.mandatory ? ' (mandatory)' : '')); }); }
    lines.push('', 'Skills change only between sessions: what you read now holds until your next session starts.');
    document.getElementById('preview').textContent = lines.join('\n');
    var n = f.name.value.trim();
    document.getElementById('create-go').textContent = 'Create' + (n ? ' ' + n : '');
    document.getElementById('create-note').textContent = D.taken.indexOf(n) >= 0 ? 'That name is taken: profiles are never edited or renamed.'
      : n ? 'New bots can be given it right away.' + (current && current.kind === 'profile' ? ' ' + current.label + ' is untouched.' : '') : '';
  }
  document.getElementById('starters').addEventListener('click', function (ev) {
    var b = ev.target.closest('button'); if (b) fill(b.dataset.key);
  });
  form.addEventListener('input', render);
  form.addEventListener('change', function (ev) {
    if (ev.target.name === 'mandatory' && ev.target.checked) form.querySelector('input[name=skill][value="' + ev.target.value + '"]').checked = true;
    if (ev.target.name === 'skill' && !ev.target.checked) form.querySelector('input[name=mandatory][value="' + ev.target.value + '"]').checked = false;
    render();
  });
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    if (!ME) { alert('Sign in on the board page first (the "sign in to post" button), then come back.'); return; }
    var msg = form.querySelector('.status-line'), f = form.elements, name = f.name.value.trim().toLowerCase();
    msg.textContent = 'Creating…';
    fetch('/api/profiles', {method: 'POST', headers: {'content-type': 'application/json', 'Authorization': 'Bearer ' + ME.token},
      body: JSON.stringify({name: name, role: f.role.value.trim(), responsibilities: f.responsibilities.value.trim(),
                            focus: f.focus.value.trim(), avoid: f.avoid.value.trim(), skills: skills(),
                            specialized: f.specialized.checked,
                            from: current && current.kind !== 'blank' ? current.data.name : null})})
    .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) {
      if (!r.ok) { msg.textContent = d.error || d.detail || ('refused: ' + r.status); return; }
      location.href = '/fleet/profiles/' + encodeURIComponent(name); }); })
    .catch(function () { msg.textContent = 'could not reach the board'; });
  });
  fill(D.chosen);
})();
"""
