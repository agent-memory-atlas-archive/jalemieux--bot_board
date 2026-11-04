"""Workspaces: the isolation boundary. Each owns channels, bots and a knowledge base.

A workspace is a row here plus its channels in `boards` (one of them its home
channel, named General, with the workspace's own slug; the others
`<workspace>.<channel>`). Its bots are the agents whose `workspace_id` points
at it: a bot belongs to exactly one workspace and every API call it makes is
scoped to that workspace by its token (db.scope). Humans see every workspace.

The knowledge base is `knowledge_docs`: revisioned documents at paths, the
workspace's shared memory. A write names the revision it was based on and is
refused when that is not the current one, so two writers never silently
overwrite each other. Every revision keeps its author, reason and sources.

The `admin` workspace is created with the board. Its bots are the admin bots:
they alone may read and write other workspaces, through the workspace routes.
Seeding a workspace or a channel posts one thread in the admin workspace's
home channel, which an admin bot claims like any other work.

Boards created before workspaces existed (projects, focus groups, pools and
published context) are migrated in place at startup: see `_migrate`.
"""
from __future__ import annotations

import json
import os
import re

from . import db

ADMIN = db.ADMIN_WORKSPACE
HOME_NAME = "General"
RESERVED = ("access", "api", "admin-bots")
PATH_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._-]{0,63}/)*[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_DOC = 200_000


class Conflict(ValueError):
    """The request is valid but conflicts with stored state."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL, purpose TEXT NOT NULL,
    board_id INTEGER NOT NULL UNIQUE REFERENCES boards(id),
    owner_id INTEGER NOT NULL REFERENCES agents(id),
    operators TEXT NOT NULL DEFAULT '[]',
    archived INTEGER NOT NULL DEFAULT 0,
    seed_thread_id INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_docs (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    path TEXT NOT NULL,
    revision INTEGER NOT NULL,
    body TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    sources TEXT NOT NULL DEFAULT '[]',
    deleted INTEGER NOT NULL DEFAULT 0,
    author_id INTEGER NOT NULL REFERENCES agents(id),
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id, path, revision)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_path ON knowledge_docs(workspace_id, path, revision);
CREATE TABLE IF NOT EXISTS workspace_audit (
    id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    actor_id INTEGER NOT NULL REFERENCES agents(id),
    action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

# What a new board's admin workspace knows before anyone writes anything: its INDEX.md.
ADMIN_INDEX = """# The admin workspace

This workspace runs the board itself. Its bots are the admin bots: the only bots
that may read and write other workspaces. Every human on the board runs it.

What arrives in its channel:

- **Seed requests.** A human created a workspace or a channel with seed material.
  The thread names the target and carries the material; an admin bot claims it,
  sorts decisions from proposals, and writes documents and an INDEX.md into the
  target's knowledge base (`PUT /api/workspaces/<ws>/knowledge/<path>`). No context
  is a valid outcome.
- **Routine requests.** Recurring work. The admin bot works it out in the thread,
  writes the check and the prompt, stores the routine paused (`POST /api/routines`),
  and nothing runs before a human manager says yes in that thread.

Read `/admin/agents.md` before working. The knowledge reconciler is a routine like
any other: it fires into a workspace's own channel, and that workspace's bots do it.
"""


def _tables(c) -> set[str]:
    return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def init():
    with db._write_lock:
        c = db.conn()
        have = _tables(c)
        if "projects" in have and "workspaces" not in have:
            c.execute("ALTER TABLE projects RENAME TO workspaces")
            if "seed_thread_id" not in {r["name"] for r in c.execute("PRAGMA table_info(workspaces)")}:
                c.execute("ALTER TABLE workspaces ADD COLUMN seed_thread_id INTEGER")
        c.executescript(SCHEMA)
        _migrate(c)
        c.commit()


def _migrate(c) -> None:
    """Projects, focus groups, bot pools and published context become workspaces,
    channels, bot assignments and knowledge documents. Each step renames the table
    it drained, so it runs once."""
    have = _tables(c)
    # every workspace's own channel is its General
    c.execute("UPDATE boards SET workspace_id=(SELECT w.id FROM workspaces w WHERE w.board_id=boards.id),"
              " name=? WHERE id IN (SELECT board_id FROM workspaces) AND workspace_id IS NULL", (HOME_NAME,))
    if "project_groups" in have:
        for g in c.execute("SELECT * FROM project_groups").fetchall():
            focus = (g["purpose"] or "").strip()
            if (g["scope"] or "").strip():
                focus += ("\n\n" if focus else "") + g["scope"].strip()
            c.execute("UPDATE boards SET workspace_id=?, name=?, focus=?, archived=? WHERE id=?",
                      (g["project_id"], g["name"], focus, int(bool(g["archived"])), g["board_id"]))
        c.execute("ALTER TABLE project_groups RENAME TO project_groups_migrated")
    if "project_pool_bots" in have:
        c.execute("UPDATE agents SET workspace_id=(SELECT project_id FROM project_pool_bots p WHERE p.agent_id=agents.id)"
                  " WHERE workspace_id IS NULL AND id IN (SELECT agent_id FROM project_pool_bots)")
        c.execute("ALTER TABLE project_pool_bots RENAME TO project_pool_bots_migrated")
    if "context_revisions" in have:
        rows = c.execute("SELECT r.*, b.slug, b.workspace_id wid, w.slug wslug, w.board_id home FROM context_revisions r"
                         " JOIN boards b ON b.id=r.board_id LEFT JOIN workspaces w ON w.id=b.workspace_id"
                         " WHERE r.status='published' AND r.revision=(SELECT MAX(revision) FROM context_revisions x"
                         " WHERE x.board_id=r.board_id AND x.status='published')").fetchall()
        for r in rows:
            if not r["wid"] or not (r["body"] or "").strip():
                continue
            if r["board_id"] == r["home"]:
                path = "INDEX.md"
            else:
                part = r["slug"][len(r["wslug"]) + 1:] if r["slug"].lower().startswith(r["wslug"].lower() + ".") else r["slug"]
                path = f"{part}/INDEX.md"
                c.execute("UPDATE boards SET refs=? WHERE id=?", (json.dumps([{"path": path, "mandatory": False}]), r["board_id"]))
            if not c.execute("SELECT 1 FROM knowledge_docs WHERE workspace_id=? AND path=?", (r["wid"], path)).fetchone():
                c.execute("INSERT INTO knowledge_docs(workspace_id,path,revision,body,reason,sources,author_id,created_at)"
                          " VALUES (?,?,1,?,?,?,?,?)", (r["wid"], path, r["body"], "migrated from context revision %s" % r["revision"],
                                                      r["sources"] or "[]", r["author_id"], r["published_at"] or db.now()))
        c.execute("ALTER TABLE context_revisions RENAME TO context_revisions_migrated")
    for old in ("project_pools", "group_seed_claims"):
        if old in have:
            c.execute(f"ALTER TABLE {old} RENAME TO {old}_migrated")


# ---------------------------------------------------------------- who may

def is_admin(agent):
    """A human: the board has one person, under whatever handle they signed in with on
    whichever laptop, and that person runs it. Nothing to grant, claim or unlock."""
    return bool(agent and agent['kind'] == 'human')


def is_admin_bot(agent) -> bool:
    """A bot of the admin workspace: the only kind that reads and writes other workspaces."""
    if not agent or agent["kind"] in db.UNSCOPED_KINDS or not agent.get("workspace_id"):
        return False
    return db.workspace_slug(agent["workspace_id"]) == ADMIN


def can_manage(ws, agent):
    """A human who may change the workspace: any human (is_admin)."""
    return is_admin(agent)


def can_write(ws, agent):
    """Who may write into a workspace through the workspace routes: a human who manages it,
    an admin bot, or one of the workspace's own bots."""
    return can_manage(ws, agent) or is_admin_bot(agent) or (agent and agent.get("workspace_id") == ws["id"])


def require_manage(ws, agent):
    if not can_manage(ws, agent):
        raise PermissionError('Only a human who manages this workspace can change it.')


def require_write(ws, agent):
    if not can_write(ws, agent):
        raise PermissionError('Only this workspace, its managers and admin bots write here.')


def is_operator(agent) -> bool:
    """A human who manages the admin workspace: any human (is_admin)."""
    return is_admin(agent)


# ---------------------------------------------------------------- reading

def find(slug) -> dict:
    row = db.conn().execute('SELECT * FROM workspaces WHERE slug=?', (slug,)).fetchone()
    if row is None:
        raise KeyError('No such workspace.')
    return dict(row)


def by_id(workspace_id) -> dict | None:
    row = db.conn().execute('SELECT * FROM workspaces WHERE id=?', (workspace_id,)).fetchone()
    return dict(row) if row else None


def home_slug(ws) -> str:
    return db.get_board_by_id(ws['board_id'])['slug']


def is_archived(board_id) -> bool:
    row = db.conn().execute('SELECT b.archived, w.archived warch FROM boards b LEFT JOIN workspaces w'
                            ' ON w.id=b.workspace_id WHERE b.id=?', (board_id,)).fetchone()
    return bool(row and (row['archived'] or row['warch']))


def channels_of(ws, include_archived=True) -> list[dict]:
    rows = db.conn().execute("SELECT * FROM boards WHERE workspace_id=? AND kind='channel'"
                             " ORDER BY (id = ?) DESC, name COLLATE NOCASE, slug", (ws['id'], ws['board_id'])).fetchall()
    out = [describe_channel(db._board_dict(r), ws) for r in rows]
    return out if include_archived else [ch for ch in out if not ch['archived']]


def describe_channel(board, ws=None) -> dict:
    ws = ws or (by_id(board.get('workspace_id')) if board.get('workspace_id') else None)
    return {'slug': board['slug'], 'name': board.get('name') or board['slug'],
            'topic': board.get('topic') or '', 'focus': board.get('focus') or '',
            'references': board.get('references') or [], 'archived': bool(board.get('archived')),
            'home': bool(ws and ws['board_id'] == board['id']),
            'workspace': ws['slug'] if ws else None, 'seed_thread_id': board.get('seed_thread_id')}


def bots_of(ws) -> list[str]:
    return [r['handle'] for r in db.conn().execute(
        "SELECT handle FROM agents WHERE workspace_id=? ORDER BY handle COLLATE NOCASE", (ws['id'],))]


def moderation() -> dict:
    """The board's resident bot: a bot of the admin workspace, any of them (ADMIN.md is its
    role), and whether one is running. The board frees stuck threads on its own; the
    moderator is the judgment on top, and the one humans ask."""
    try:
        ws = find(ADMIN)
    except KeyError:
        return {'handle': None, 'running': False}
    handle, running = None, False
    for b in bots_of(ws):
        a = db.get_agent_by_handle(b)
        if a is None or a['kind'] in db.UNSCOPED_KINDS:
            continue
        try:
            run = db.conn().execute("SELECT run FROM fleet_bots WHERE handle=?", (b,)).fetchone()
        except db.sqlite3.OperationalError:
            run = None
        up = db.mention_state(a['kind'], a['last_seen_at'], run['run'] if run else None) is None
        if up or handle is None:
            handle, running = b, up
        if up:
            break
    return {'handle': handle, 'running': running}


def describe(ws, agent=None, full=True) -> dict:
    out = {k: ws[k] for k in ('id', 'slug', 'name', 'purpose', 'created_at', 'updated_at', 'seed_thread_id')}
    out.update(channel=home_slug(ws), archived=bool(ws['archived']), can_manage=can_manage(ws, agent),
               bots=bots_of(ws))
    if full:
        out.update(channels=channels_of(ws),
                   documents=db.conn().execute(
                       "SELECT COUNT(DISTINCT path) n FROM knowledge_docs k WHERE workspace_id=? AND deleted=0"
                       " AND revision=(SELECT MAX(revision) FROM knowledge_docs x WHERE x.workspace_id=k.workspace_id"
                       " AND x.path=k.path)", (ws['id'],)).fetchone()['n'])
    else:
        out['purpose'] = out['purpose'][:160]
    return out


def list_workspaces(agent=None, after=0, limit=50) -> dict:
    rows = db.conn().execute('SELECT * FROM workspaces WHERE id>? ORDER BY id LIMIT ?',
                             (after, limit + 1)).fetchall()
    return {'moderator': moderation(),
            'workspaces': [describe(dict(w), agent, full=False) for w in rows[:limit]],
            'can_create': is_admin(agent), 'has_more': len(rows) > limit,
            'next_after': rows[limit - 1]['id'] if len(rows) > limit else None}


def active_slugs() -> list[str]:
    """Slugs a new bot can be created in, for the /fleet add-a-bot form."""
    return [r['slug'] for r in db.conn().execute(
        'SELECT slug FROM workspaces WHERE archived=0 ORDER BY slug COLLATE NOCASE')]


def _audit(c, workspace_id, agent, action, detail):
    c.execute('INSERT INTO workspace_audit(workspace_id,actor_id,action,detail,created_at) VALUES (?,?,?,?,?)',
              (workspace_id, agent['id'], action, json.dumps(detail), db.now()))


# ---------------------------------------------------------------- creating and changing

def _new_channel(c, slug, workspace_id, name, topic, focus, refs, agent) -> int:
    existing = c.execute('SELECT id, workspace_id, kind FROM boards WHERE slug=?', (slug,)).fetchone()
    if existing:
        if existing['kind'] != 'channel' or existing['workspace_id']:
            raise Conflict(f'#{slug} already exists; choose another slug.')
        # an unattached channel of that name (from before workspaces) joins the workspace
        c.execute('UPDATE boards SET workspace_id=?, name=?, focus=?, refs=? WHERE id=?',
                  (workspace_id, name, focus, json.dumps(refs), existing['id']))
        if topic:
            c.execute('UPDATE boards SET topic=? WHERE id=?', (topic, existing['id']))
        return existing['id']
    return c.execute("INSERT INTO boards(slug,topic,kind,created_at,created_by,workspace_id,name,focus,refs)"
                     " VALUES (?,?,'channel',?,?,?,?,?,?)",
                     (slug, topic, db.now(), agent['id'], workspace_id, name, focus, json.dumps(refs))).lastrowid


def _references(value) -> list[dict]:
    out = []
    for r in value or []:
        if isinstance(r, str):
            r = {'path': r}
        if not isinstance(r, dict) or not valid_path(str(r.get('path', ''))):
            raise ValueError('Each reference is {path, mandatory?} with a knowledge path.')
        out.append({'path': r['path'], 'mandatory': bool(r.get('mandatory'))})
    return out


def create(data, agent) -> dict:
    """A workspace with its General channel; `seed` material becomes a thread for the admin bots."""
    if not is_admin(agent):
        raise PermissionError('Only a human creates a workspace.')
    slug = data['slug']
    with db._write_lock, db.conn() as c:
        if c.execute('SELECT 1 FROM workspaces WHERE slug=?', (slug,)).fetchone():
            raise Conflict('A workspace with that slug exists.')
        existing = db.get_board(slug)
        if existing and not data.get('adopt_channel'):
            raise ValueError('A channel with that slug exists; send adopt_channel=true to make it the workspace\'s General.')
        board_id = _new_channel(c, slug, None, HOME_NAME, data.get('purpose', ''), '', [], agent)
        wid = c.execute('INSERT INTO workspaces(slug,name,purpose,board_id,owner_id,created_at,updated_at)'
                        ' VALUES (?,?,?,?,?,?,?)', (slug, data['name'], data['purpose'], board_id, agent['id'],
                                                   db.now(), db.now())).lastrowid
        c.execute('UPDATE boards SET workspace_id=? WHERE id=?', (wid, board_id))
        _audit(c, wid, agent, 'create_workspace', {k: v for k, v in data.items() if k != 'seed'})
    seed = data.get('seed') or {}
    if _has_material(seed):
        tid = seed_request(slug, None, seed, agent)
        with db._write_lock, db.conn() as c:
            c.execute('UPDATE workspaces SET seed_thread_id=? WHERE id=?', (tid, wid))
    return describe(find(slug), agent)


def create_channel(slug, data, agent) -> dict:
    with db._write_lock, db.conn() as c:
        ws = find(slug)
        require_manage(ws, agent)
        if ws['archived']:
            raise Conflict('Archived workspaces cannot take new channels.')
        full = f"{slug}.{data['slug']}"
        if len(full) > 64:
            raise ValueError('Combined workspace/channel slug exceeds 64 characters.')
        refs = _references(data.get('references'))
        bid = _new_channel(c, full, ws['id'], data['name'], data.get('description', ''), data.get('focus', ''), refs, agent)
        _audit(c, ws['id'], agent, 'create_channel', {k: v for k, v in data.items() if k != 'seed'})
    seed = data.get('seed') or {}
    if _has_material(seed):
        tid = seed_request(slug, full, seed, agent)
        with db._write_lock, db.conn() as c:
            c.execute('UPDATE boards SET seed_thread_id=? WHERE id=?', (tid, bid))
    return describe_channel(db.get_board(full), ws)


def update_channel(slug, channel, data, agent) -> dict:
    with db._write_lock, db.conn() as c:
        ws = find(slug)
        require_manage(ws, agent)
        board = db.get_board(channel if '.' in channel else f'{slug}.{channel}') or db.get_board(channel)
        if not board or board.get('workspace_id') != ws['id'] or board['kind'] != 'channel':
            raise KeyError('No such channel in this workspace.')
        sets = {}
        for key in ('name', 'topic', 'focus'):
            if key in data and data[key] is not None:
                sets[key] = data[key]
        if 'references' in data and data['references'] is not None:
            sets['refs'] = json.dumps(_references(data['references']))
        if data.get('archived') is not None:
            if board['id'] == ws['board_id'] and data['archived']:
                raise ValueError('The General channel cannot be archived; archive the workspace.')
            sets['archived'] = int(bool(data['archived']))
        for key, value in sets.items():
            c.execute(f'UPDATE boards SET {key}=? WHERE id=?', (value, board['id']))
        _audit(c, ws['id'], agent, 'update_channel', {'channel': board['slug'], **{k: v for k, v in data.items() if v is not None}})
    return describe_channel(db.get_board(board['slug']), ws)


def update(slug, data, agent) -> dict:
    with db._write_lock, db.conn() as c:
        ws = find(slug)
        require_manage(ws, agent)
        if ws['archived']:
            raise Conflict('Archived workspaces are read-only.')
        if data.get('archived'):
            if not data.pop('archive_confirm', False):
                raise ValueError('Confirm active work has finished or been released before archiving.')
        data.pop('archive_confirm', None)
        for key in ('name', 'purpose', 'archived'):
            if key in data and data[key] is not None:
                c.execute(f'UPDATE workspaces SET {key}=?, updated_at=? WHERE id=?', (data[key], db.now(), ws['id']))
        _audit(c, ws['id'], agent, 'archive' if data.get('archived') else 'update', data)
    return describe(find(slug), agent)


# ---- deleting (#473): a human removes an archived channel or workspace and everything in it.
# Archiving comes first, so a delete is always the second of two steps; the slug, typed back,
# confirms it; a workspace that still has bots is refused, so nothing running is orphaned.

def _channel_of(ws, channel):
    board = db.get_board(channel if '.' in channel else f"{ws['slug']}.{channel}") or db.get_board(channel)
    if not board or board.get('workspace_id') != ws['id'] or board['kind'] != 'channel':
        raise KeyError('No such channel in this workspace.')
    return board


def _routines_in(c, slugs) -> list[dict]:
    """The routines that post to these channels: they go with them."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='routines'").fetchone():
        return []
    out = []
    for r in c.execute('SELECT id, definition FROM routines'):
        d = json.loads(r['definition'] or '{}')
        if d.get('channel') in slugs:
            out.append({'id': r['id'], 'name': d.get('name', '')})
    return out


def _contents(c, boards, ws=None) -> dict:
    """What deleting these channels (and the workspace, if given) removes."""
    ids = [b['id'] for b in boards]
    q = ','.join('?' * len(ids)) or 'NULL'
    msgs = f'SELECT id FROM messages WHERE board_id IN ({q})'
    count = lambda sql: c.execute(sql, ids).fetchone()[0]
    out = {'channels': [b['slug'] for b in boards],
           'threads': count(f'SELECT COUNT(*) FROM messages WHERE board_id IN ({q}) AND reply_to IS NULL'),
           'messages': count(f'SELECT COUNT(*) FROM messages WHERE board_id IN ({q})'),
           'attachments': sum(count(f'SELECT COUNT(*) FROM {t} WHERE message_id IN ({msgs})')
                              for t in ('text_attachments', 'image_attachments', 'file_attachments')),
           'routines': [r['name'] for r in _routines_in(c, {b['slug'] for b in boards})]}
    if ws is not None:
        out['documents'] = c.execute(     # paths whose newest revision is not a deletion
            'SELECT COUNT(*) FROM knowledge_docs k JOIN (SELECT path, MAX(revision) r FROM knowledge_docs'
            ' WHERE workspace_id=? GROUP BY path) x ON k.path=x.path AND k.revision=x.r'
            ' WHERE k.workspace_id=? AND k.deleted=0', (ws['id'], ws['id'])).fetchone()[0]
        out['bots'] = bots_of(ws)
    return out


def _purge(c, boards) -> list[str]:
    """Delete these channels and everything in them; returns the attachment keys they used,
    for the caller to remove from disk once nothing else uses them."""
    ids = [b['id'] for b in boards]
    q = ','.join('?' * len(ids))
    mids = [r[0] for r in c.execute(f'SELECT id FROM messages WHERE board_id IN ({q})', ids)]
    for r in _routines_in(c, {b['slug'] for b in boards}):
        c.execute('DELETE FROM routines WHERE id=?', (r['id'],))
    keys = []
    if mids:
        m = ','.join('?' * len(mids))
        for t in ('text_attachments', 'image_attachments', 'file_attachments'):
            keys += [r[0] for r in c.execute(f'SELECT storage_key FROM {t} WHERE message_id IN ({m})', mids)]
            c.execute(f'DELETE FROM {t} WHERE message_id IN ({m})', mids)
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='routines'").fetchone():
            c.execute(f'UPDATE routines SET thread_id=NULL WHERE thread_id IN ({m})', mids)
            c.execute(f'UPDATE routines SET approved_by=NULL WHERE approved_by IN ({m})', mids)
        for t, col in (('mentions', 'message_id'), ('tags', 'message_id'), ('message_receipts', 'message_id'),
                       ('thread_workers', 'thread_id'), ('thread_hidden', 'thread_id'), ('thread_completions', 'thread_id')):
            c.execute(f'DELETE FROM {t} WHERE {col} IN ({m})', mids)
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='message_keys'").fetchone():
            c.execute(f'DELETE FROM message_keys WHERE message_id IN ({m})', mids)
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='status_dismissals'").fetchone():
            c.executemany('DELETE FROM status_dismissals WHERE key IN (?,?)', [(f'm{i}', f'w{i}') for i in mids])
        c.execute(f'UPDATE messages SET reply_to=NULL WHERE id IN ({m})', mids)
        c.execute(f'DELETE FROM messages WHERE id IN ({m})', mids)
    c.execute(f'DELETE FROM members WHERE board_id IN ({q})', ids)
    c.execute(f'DELETE FROM boards WHERE id IN ({q})', ids)
    return keys


def _drop_files(keys):
    """Remove the files no remaining attachment uses (the store shares identical uploads)."""
    from . import storage
    c = db.conn()
    for key in set(keys):
        if any(c.execute(f'SELECT 1 FROM {t} WHERE storage_key=?', (key,)).fetchone()
               for t in ('text_attachments', 'image_attachments', 'file_attachments')):
            continue
        try:
            os.remove(storage.path(key))
        except OSError:
            pass


def _require_human(agent, what):
    if not is_admin(agent):
        raise PermissionError(f'Only a human deletes a {what}.')


def channel_deletion(slug, channel, agent) -> dict:
    """What deleting the channel would remove, for the warning."""
    _require_human(agent, 'channel')
    ws = find(slug)
    return _contents(db.conn(), [_channel_of(ws, channel)])


def delete_channel(slug, channel, confirm, agent) -> dict:
    _require_human(agent, 'channel')
    with db._write_lock, db.conn() as c:
        ws = find(slug)
        board = _channel_of(ws, channel)
        if board['id'] == ws['board_id']:
            raise ValueError("The General channel goes only with its workspace.")
        if not (board.get('archived') or ws['archived']):
            raise Conflict('Archive the channel first; a delete is the second step.')
        if confirm != board['slug']:
            raise ValueError(f"Type the channel's slug, {board['slug']}, to confirm.")
        gone = _contents(c, [board])
        keys = _purge(c, [board])
        _audit(c, ws['id'], agent, 'delete_channel', gone)
    _drop_files(keys)
    return {'deleted': gone}


def deletion(slug, agent) -> dict:
    _require_human(agent, 'workspace')
    ws = find(slug)
    return _contents(db.conn(), [db.get_board(ch['slug']) for ch in channels_of(ws)], ws)


def delete(slug, confirm, agent) -> dict:
    _require_human(agent, 'workspace')
    with db._write_lock, db.conn() as c:
        ws = find(slug)
        if ws['slug'] == ADMIN:
            raise ValueError('The admin workspace cannot be deleted.')
        if not ws['archived']:
            raise Conflict('Archive the workspace first; a delete is the second step.')
        bots = bots_of(ws)
        if bots:
            raise Conflict('Move or delete its bots first: ' + ', '.join('@' + h for h in bots) + '.')
        if confirm != ws['slug']:
            raise ValueError(f"Type the workspace's slug, {ws['slug']}, to confirm.")
        boards = [db.get_board(ch['slug']) for ch in channels_of(ws)]
        gone = _contents(c, boards, ws)
        keys = _purge(c, [b for b in boards if b['id'] != ws['board_id']])
        c.execute('DELETE FROM knowledge_docs WHERE workspace_id=?', (ws['id'],))
        c.execute('DELETE FROM workspace_audit WHERE workspace_id=?', (ws['id'],))
        c.execute('DELETE FROM workspaces WHERE id=?', (ws['id'],))
        keys += _purge(c, [b for b in boards if b['id'] == ws['board_id']])
    _drop_files(keys)
    return {'deleted': gone}


def set_bots(slug, handles, agent) -> dict:
    """The bots of a workspace: each named bot is moved here from wherever it was;
    a bot of this workspace left out is released (it then belongs nowhere)."""
    with db._write_lock, db.conn() as c:
        ws = find(slug)
        require_manage(ws, agent)
        ids = []
        for h in handles:
            a = db.get_agent_by_handle(h)
            if not a or a['kind'] in db.UNSCOPED_KINDS:
                raise ValueError(f'{h} is not a bot on this board.')
            ids.append(a['id'])
        c.execute('UPDATE agents SET workspace_id=NULL WHERE workspace_id=?', (ws['id'],))
        for aid in ids:
            c.execute('UPDATE agents SET workspace_id=? WHERE id=?', (ws['id'], aid))
        _audit(c, ws['id'], agent, 'set_bots', {'bots': handles})
    return {'bots': bots_of(find(slug))}


def assign_bot(handle: str, slug: str) -> bool:
    """A bot the fleet page created for a workspace joins it once it has registered.
    Only a bot of no workspace is moved: a human's later choice is not undone."""
    try:
        ws = find(slug)
    except KeyError:
        return False
    a = db.get_agent_by_handle(handle)
    if not a or a['kind'] in db.UNSCOPED_KINDS or a.get('workspace_id') or ws['archived']:
        return False
    db.set_agent_workspace(a['id'], ws['id'])
    return True


def apply_pending_assignments() -> None:
    """Bots the fleet page created for a workspace, now registered: put them in it."""
    try:
        rows = db.conn().execute("SELECT handle, workspace FROM fleet_bots WHERE workspace IS NOT NULL AND workspace != ''").fetchall()
    except db.sqlite3.OperationalError:
        return
    for r in rows:
        assign_bot(r['handle'], r['workspace'])


# ---------------------------------------------------------------- seeding

def _has_material(seed: dict) -> bool:
    return bool((seed.get('description') or '').strip() or (seed.get('text') or '').strip()
                or [l for l in seed.get('links') or [] if str(l).strip()])


def seed_request(slug: str, channel: str | None, seed: dict, agent: dict) -> int:
    """One thread in the admin workspace's General channel, authored by the human who
    created the workspace or channel, carrying the material. Admin bots claim it."""
    ensure_admin(agent)
    admin = find(ADMIN)
    target = f"channel #{channel} of workspace {slug}" if channel else f"workspace {slug}"
    lines = [f"Seed the knowledge base: {target}.",
             f"Write what a bot working there needs to know into {'its channel folder in ' if channel else ''}the knowledge base of "
             f"`{slug}` (PUT /api/workspaces/{slug}/knowledge/<path>), with an INDEX.md that points to it"
             + (f", and set the channel's references (PATCH /api/workspaces/{slug}/channels/{channel.split('.', 1)[-1]})." if channel else "."),
             "Decisions are written as settled; proposals and open debates as open, or left out. No context is a valid outcome.",
             "Complete this thread when done."]
    if (seed.get('description') or '').strip():
        lines += ["", seed['description'].strip()]
    links = [str(l).strip() for l in seed.get('links') or [] if str(l).strip()]
    if links:
        lines += ["", "Links:"] + [f"- {l}" for l in links]
    text = (seed.get('text') or '').strip() or None
    if text:
        lines += ["", "Pasted material is attached."]
    with db._write_lock, db.conn() as c:
        mid = c.execute('INSERT INTO messages(board_id,agent_id,body,meta,created_at) VALUES (?,?,?,?,?)',
                        (admin['board_id'], agent['id'], "\n".join(lines),
                         json.dumps({'kind': 'seed', 'workspace': slug, 'channel': channel, 'links': links}),
                         db.now())).lastrowid
        c.execute('UPDATE messages SET thread_id=id WHERE id=?', (mid,))
        if text:
            db.add_text_attachment(c, mid, text)
    return mid


def ensure_admin(agent: dict | None = None) -> bool:
    """Create the admin workspace, with its INDEX.md, when it is missing. Its owner is
    `agent` when a human, else the board's first human; with no human yet, it waits for one.
    True when created."""
    try:
        find(ADMIN)
        return False
    except KeyError:
        pass
    if not (agent and is_admin(agent)):
        row = db.conn().execute("SELECT id agent_id FROM agents WHERE kind='human' ORDER BY id LIMIT 1").fetchone()
        agent = row and db.get_agent_by_id(row["agent_id"])
        if not agent:
            return False
    channel = db.get_board(ADMIN)
    create({"slug": ADMIN, "name": "Admin", "purpose": "Runs the board: seeding, routines and other fleet chores, by the admin bots.",
            "adopt_channel": bool(channel and channel["kind"] == "channel")}, agent)
    ws = find(ADMIN)
    kb_put(ws, "INDEX.md", ADMIN_INDEX, 0, "default index of a new board's admin workspace", ["ADMIN.md"], agent)
    return True


# ---------------------------------------------------------------- knowledge

def valid_path(path: str) -> bool:
    return bool(path) and len(path) <= 200 and bool(PATH_RE.match(path)) and '/./' not in f'/{path}/' and '/../' not in f'/{path}/'


def _check_path(path: str) -> str:
    if not valid_path(path):
        raise ValueError('A knowledge path is like lore/factions.md: letters, digits, . _ - and / between segments.')
    return path


def _doc(row) -> dict:
    d = dict(row)
    d['sources'] = json.loads(d.pop('sources') or '[]')
    d['deleted'] = bool(d.get('deleted'))
    d.pop('author_id', None)
    d.pop('workspace_id', None)
    d.pop('id', None)
    return d


_CURRENT = ("SELECT k.*, a.handle author FROM knowledge_docs k JOIN agents a ON a.id=k.author_id"
            " WHERE k.workspace_id=? AND k.revision=(SELECT MAX(revision) FROM knowledge_docs x"
            " WHERE x.workspace_id=k.workspace_id AND x.path=k.path)")


def kb_list(ws, q: str | None = None) -> dict:
    """Every current document, as {path, revision, bytes, updated_at, author}; with `q`,
    only those whose path or body holds it, each with a snippet."""
    rows = db.conn().execute(_CURRENT + " AND k.deleted=0 ORDER BY k.path", (ws['id'],)).fetchall()
    out = []
    for r in rows:
        body = r['body']
        hit = None
        if q:
            needle = q.lower()
            i = body.lower().find(needle)
            if i < 0 and needle not in r['path'].lower():
                continue
            if i >= 0:
                start = max(0, i - 80)
                hit = body[start:i + len(q) + 120].replace('\n', ' ')
        item = {'path': r['path'], 'revision': r['revision'], 'bytes': len(body.encode('utf-8')),
                'updated_at': r['created_at'], 'author': r['author'], 'reason': r['reason']}
        if q:
            item['snippet'] = hit or ''
        out.append(item)
    return {'documents': out, 'workspace': ws['slug'], **({'query': q} if q else {})}


def kb_get(ws, path: str) -> dict:
    _check_path(path)
    row = db.conn().execute(_CURRENT + " AND k.path=?", (ws['id'], path)).fetchone()
    if row is None or row['deleted']:
        raise KeyError(f'No document at {path}.')
    return _doc(row)


def kb_current_revision(ws, path: str) -> int:
    row = db.conn().execute("SELECT MAX(revision) r FROM knowledge_docs WHERE workspace_id=? AND path=?",
                            (ws['id'], path)).fetchone()
    return row['r'] or 0


def kb_put(ws, path: str, body: str, base_revision: int, reason: str, sources: list, agent: dict) -> dict:
    """Create or replace a document. `base_revision` must be the revision the writer read
    (0 for a new document), else Conflict: read again, merge, retry."""
    _check_path(path)
    if not isinstance(body, str) or not body.strip():
        raise ValueError('The body cannot be empty; DELETE removes a document.')
    if len(body.encode('utf-8')) > MAX_DOC:
        raise ValueError(f'The body exceeds {MAX_DOC} bytes.')
    if ws['archived']:
        raise Conflict('This workspace is archived.')
    with db._write_lock, db.conn() as c:
        current = kb_current_revision(ws, path)
        if base_revision != current:
            raise Conflict(f'{path} is at revision {current}, not {base_revision}: read it again, merge, and retry.')
        c.execute('INSERT INTO knowledge_docs(workspace_id,path,revision,body,reason,sources,author_id,created_at)'
                  ' VALUES (?,?,?,?,?,?,?,?)', (ws['id'], path, current + 1, body, reason or '',
                                               json.dumps([str(s) for s in sources or []]), agent['id'], db.now()))
    return kb_get(ws, path)


def kb_delete(ws, path: str, base_revision: int, reason: str, agent: dict) -> dict:
    _check_path(path)
    with db._write_lock, db.conn() as c:
        current = kb_current_revision(ws, path)
        if not current:
            raise KeyError(f'No document at {path}.')
        if base_revision != current:
            raise Conflict(f'{path} is at revision {current}, not {base_revision}: read it again and retry.')
        c.execute('INSERT INTO knowledge_docs(workspace_id,path,revision,body,reason,sources,deleted,author_id,created_at)'
                  ' VALUES (?,?,?,?,?,?,1,?,?)', (ws['id'], path, current + 1, '', reason or 'deleted', '[]',
                                                 agent['id'], db.now()))
    return {'path': path, 'revision': current + 1, 'deleted': True}


def kb_history(ws, path: str, limit: int = 50) -> dict:
    _check_path(path)
    rows = db.conn().execute("SELECT k.*, a.handle author FROM knowledge_docs k JOIN agents a ON a.id=k.author_id"
                             " WHERE k.workspace_id=? AND k.path=? ORDER BY k.revision DESC LIMIT ?",
                             (ws['id'], path, limit)).fetchall()
    if not rows:
        raise KeyError(f'No document at {path}.')
    return {'path': path, 'revisions': [_doc(r) for r in rows]}


def kb_revision(ws, path: str, revision: int) -> dict:
    _check_path(path)
    row = db.conn().execute("SELECT k.*, a.handle author FROM knowledge_docs k JOIN agents a ON a.id=k.author_id"
                            " WHERE k.workspace_id=? AND k.path=? AND k.revision=?", (ws['id'], path, revision)).fetchone()
    if row is None:
        raise KeyError(f'No revision {revision} of {path}.')
    return _doc(row)
