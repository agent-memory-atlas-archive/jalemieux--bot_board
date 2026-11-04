"""System status: the fleet, the workspaces and the routines in one rollup (#92).

It reads what /fleet, /workspaces and /routines read, and says in one list what
needs a human, each item pointing to where it is fixed. GET /api/status returns it;
/fleet shows it at the top.

The only thing stored is which items a human dismissed. A dismissed item leaves the
list (and the bell count) while it holds; once its cause is gone the dismissal is
forgotten, so the same trouble coming back later shows again.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from . import db, fleet, workspaces
from .web import ago

RUNNER_STALE = 15 * 60      # an enabled routine whose runner has not been seen for this long is not running


DISMISS_SCHEMA = """
CREATE TABLE IF NOT EXISTS status_dismissals (
    key TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    by_agent INTEGER NOT NULL REFERENCES agents(id),
    at TEXT NOT NULL
);
"""
# Counts and ages change while the trouble stays the same ("last heard 5 min ago"): the
# key leaves them out, so a dismissal outlives a refresh of the wording.
_VOLATILE = re.compile(r"\d+(\.\d+)?\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)?\b")


def key_of(text: str, href: str) -> str:
    return hashlib.sha1((href + "\n" + _VOLATILE.sub("#", text)).encode()).hexdigest()[:16]


def _token_when(due: dict) -> str:
    return f"expires in {due['days_left']} days" if due["days_left"] > 0 else "has expired"


def _item(text: str, href: str, where: str) -> dict:
    return {"text": text, "href": href, "where": where, "key": key_of(text, href)}


def _ensure_table(c) -> None:
    c.executescript(DISMISS_SCHEMA)


def dismiss(key: str, agent: dict) -> dict:
    """Hide the item with this key until its cause is gone. KeyError when no current item has it."""
    if key.startswith("m") and key[1:].isdigit():     # a waiting or mention item: its message
        msg = db.get_message(int(key[1:]))
        item = msg and {"text": msg["body"][:200]}
    else:
        item = next((a for a in rollup()["attention"] if a["key"] == key), None)
    if item is None:
        raise KeyError("no current alert has that key")
    with db._write_lock, db.conn() as c:
        _ensure_table(c)
        c.execute("INSERT OR REPLACE INTO status_dismissals(key,text,by_agent,at) VALUES (?,?,?,?)",
                  (key, item["text"], agent["id"], db.now()))
    return rollup(agent=agent)


def restore(key: str, agent: dict | None = None) -> dict:
    with db._write_lock, db.conn() as c:
        _ensure_table(c)
        c.execute("DELETE FROM status_dismissals WHERE key=?", (key,))
    return rollup(agent=agent)


def _split(attention: list) -> tuple[list, list]:
    """(shown, dismissed); forgets dismissals whose item is gone."""
    c = db.conn()
    with db._write_lock:
        _ensure_table(c)
    rows = {r["key"]: dict(r) for r in c.execute(
        "SELECT d.key, d.at, a.handle FROM status_dismissals d LEFT JOIN agents a ON a.id=d.by_agent")}
    current = {a["key"] for a in attention}
    gone = [k for k in rows if k not in current and k[:1] not in ("m", "w")]   # m<id>, w<id>: a message, see asks()
    if gone:
        with db._write_lock, c:
            c.executemany("DELETE FROM status_dismissals WHERE key=?", [(k,) for k in gone])
    shown = [a for a in attention if a["key"] not in rows]
    hidden = [{**a, "dismissed_by": rows[a["key"]]["handle"], "dismissed_at": rows[a["key"]]["at"]}
              for a in attention if a["key"] in rows]
    return shown, hidden


def _fleet(hosts: list, attention: list) -> list:
    out = []
    due = fleet.claude_token_due()
    if due:
        attention.append(_item(f"The fleet's claude token {_token_when(due)}: replace {fleet.CLAUDE_TOKEN}"
                               " with a new one from claude setup-token", "/fleet#secrets", "fleet"))
    troubles = {}      # why -> [(box, handle)]: the same trouble on many bots is one item
    for h in hosts:
        if not h["reporting"]:
            attention.append(_item(f"{h['name']} is not reporting: last heard {ago(h['reported_at'])}",
                                   f"/fleet#box-{h['name']}", "fleet"))
        # a login missing here is one item, not one more per bot it holds
        out_of = {c["name"] for c in h.get("credentials") or [] if c["kind"] == "login" and c["state"] != "ok"}
        for c in h.get("credentials") or []:
            if c["state"] in ("missing", "expired", "rejected") and (c.get("held") or c["kind"] == "login"):
                held = f", {c['held']} bot{'' if c['held'] == 1 else 's'} held" if c.get("held") else ""
                attention.append(_item(f"{c['name']} {c['state']} on {h['name']}{held}"
                                       + (f": {c['detail']}" if c.get("detail") else ""),
                                       f"/fleet#box-{h['name']}", "fleet"))
        bots = []
        limited = {}
        for b in h["bots"]:
            st = b.get("state") or fleet.bot_state(b, h["reporting"])
            row = {k: b.get(k) for k in ("handle", "harness", "status", "want", "problem", "workspace")}
            row.update(state=st, up=st["can_work"], health=b.get("health") or "")
            bots.append(row)
            if st["state"] == "out of usage" and h["reporting"]:
                limited.setdefault(b.get("harness") or "harness", []).append(b)
                continue
            why = b.get("problem") or (row["health"] if b["status"].startswith(fleet.UP) else "")
            # a bot nobody asked for and that is simply stopped is not a problem; neither is one on a
            # box gone dark (the box's own item covers it) or one held by a login the box lacks (ditto)
            held = st["state"] == "needs login" and (b.get("harness") or "") in out_of
            if why and h["reporting"] and not held and (b["status"].startswith(fleet.UP) or b.get("want")):
                troubles.setdefault(why, []).append((h["name"], b["handle"]))
        for harness, blocked in limited.items():
            names = ", ".join("@" + b["handle"] for b in blocked)
            attention.append(_item(f"{harness} usage limit reached on {h['name']}: "
                                   f"{len(blocked)} bot(s) blocked ({names}). {blocked[0]['health']}",
                                   f"/fleet#box-{h['name']}", "fleet"))
        out.append({"name": h["name"], "reporting": h["reporting"], "reported_at": h["reported_at"],
                    "load": h.get("load"), "cores": h.get("cores"), "credentials": h.get("credentials") or [],
                    "bots": bots, "up": sum(b["up"] for b in bots)})
    for why, bots in troubles.items():
        boxes = ", ".join(dict.fromkeys(box for box, _ in bots))
        if len(bots) == 1:
            attention.append(_item(f"@{bots[0][1]} on {boxes}: {why}", f"/fleet#bot-{bots[0][1]}", "fleet"))
        else:
            attention.append(_item(f"{len(bots)} bots ({', '.join('@' + h for _, h in bots)}) on {boxes}: {why}",
                                   "/fleet#fleet-attention", "fleet"))
    return out


def _workspaces(hosts: list, attention: list) -> list:
    # up: can take work now (#100); a logged-out bot on a reporting box is not up
    can = lambda h, b: (b.get("state") or fleet.bot_state(b, h["reporting"]))["can_work"]
    up = {b["handle"].lower() for h in hosts for b in h["bots"] if can(h, b)}
    out = []
    try:
        rows = db.conn().execute("SELECT * FROM workspaces WHERE archived=0 ORDER BY slug").fetchall()
    except db.sqlite3.OperationalError:
        return out
    for w in rows:
        w = dict(w)
        bots = workspaces.bots_of(w)
        working = [b for b in bots if b.lower() in up]
        if not bots:
            if w["slug"] == workspaces.ADMIN:
                attention.append(_item(f"workspace {w['slug']} has no bot: add one on /fleet, so seed and routine "
                                       "requests are answered", f"/workspaces/{w['slug']}", "workspaces"))
            else:
                attention.append(_item(f"workspace {w['slug']} has no bot", f"/workspaces/{w['slug']}", "workspaces"))
        elif not working:
            attention.append(_item(f"workspace {w['slug']}: none of its bots is up "
                                   f"({', '.join('@' + b for b in bots)})", f"/workspaces/{w['slug']}", "workspaces"))
        out.append({"slug": w["slug"], "name": w["name"], "channel": workspaces.home_slug(w),
                    "bots": bots, "up": working})
    return out


def _routines(attention: list) -> list:
    from . import routines     # loads bin/routines; only when asked
    try:
        rows = routines.list_routines(full=True)
    except db.sqlite3.OperationalError:     # a board without routines yet
        return []
    runner = db.get_agent_by_handle(routines.cli.ME)
    seen = db.seconds_since(runner["last_seen_at"]) if runner and runner.get("last_seen_at") else None
    out = []
    for r in rows:
        last = r["jobs"][0] if r.get("jobs") else None
        row = {"id": r["id"], "name": r["name"], "state": r["state"], "channel": r["channel"],
               "every": r.get("every"), "failure": r.get("failure"), "problem": r.get("problem") or "",
               "last_job": last and {"at": last["created_at"], "done_by": last["done_by"]},
               "last_test": r.get("last_test") and {"ok": r["last_test"].get("ok"), "at": r["last_test"].get("at")}}
        href = f"/routines#r={r['id']}"
        if r["state"] == "failed":
            attention.append(_item(f"routine {r['name']} failed: {r.get('failure') or 'see its page'}", href, "routines"))
        elif r["state"] == "enabled" and row["problem"]:
            attention.append(_item(f"routine {r['name']}: {row['problem']}", href, "routines"))
        out.append(row)
    waiting = [r for r in rows if r.get("waiting") and not r.get("problem")]
    ups = [r for r in rows if r.get("upgrade")]
    if waiting or ups:
        # versions nobody has said yes to, and drafted recipe upgrades: one item, one page, one click each (#380, #242)
        n = len(waiting) + len(ups)
        attention.append(_item(f"{n} routine change{'s' if n != 1 else ''} wait{'' if n != 1 else 's'} for your yes: "
                               + ", ".join(f"{r['name']} v{r['version']}" for r in waiting)
                               + (", " if waiting and ups else "") + ", ".join(f"{r['name']} (recipe v{r['upgrade']['to']})" for r in ups),
                               "/routines#approvals", "routines"))
    stalled = [r["name"] for r in out if r["state"] == "enabled"]
    if stalled and (seen is None or seen > RUNNER_STALE):    # one item: it is the runner that is down
        attention.append(_item(f"{len(stalled)} enabled routine(s) ({', '.join(stalled)}) but the runner "
                               f"(@{routines.cli.ME}) " + ("has never connected" if seen is None
                                                          else f"was last seen {int(seen // 60)} min ago"),
                               "/routines", "routines"))
    return out


MENTION_DAYS = 7     # how far back the Mentions list looks


def _needs(meta: dict, handle: str) -> bool:
    n = meta.get("needs")
    names = n if isinstance(n, list) else [n] if isinstance(n, str) else []
    return handle.lower() in {str(x).lower().lstrip("@") for x in names}


def asks(agent: dict) -> dict:
    """What bots wait on this human for (#223). waiting: messages marked meta.needs=<handle>
    (or tagged needs-human with an @mention of them); each stays until the human posts in that
    thread after it, its author posts meta.resolved there, the thread is completed after it, or it
    is dismissed. mentions: the
    other @mentions of the last MENTION_DAYS days the human has not posted after; they do not
    count on the bell."""
    c = db.conn()
    me, handle = agent["id"], agent["handle"]
    since = (datetime.now(timezone.utc) - timedelta(days=MENTION_DAYS)).isoformat(timespec="seconds")
    marks = {r["key"] for r in c.execute("SELECT key FROM status_dismissals WHERE key LIKE 'm%' OR key LIKE 'w%'")} \
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='status_dismissals'").fetchone() else set()
    gone = {k for k in marks if k[0] == "m"}          # dismissed by the human: off both lists
    settled = {k for k in marks if k[0] == "w"}       # its thread was completed: no longer an ask (#448)
    # the last time this human posted in each thread, and where an asker said it is resolved
    mine = {r["t"]: r["last"] for r in c.execute(
        "SELECT thread_id t, MAX(id) last FROM messages WHERE agent_id=? GROUP BY thread_id", (me,))}
    def answered(m):
        return mine.get(m["thread_id"] or m["id"], 0) > m["id"]
    def resolved(m):     # its asker said so, or the thread was completed after it: it blocks nobody (#448)
        t = m["thread_id"] or m["id"]
        if f"w{m['id']}" in settled:
            return True
        return c.execute("SELECT 1 FROM messages WHERE thread_id=? AND id>? AND agent_id=? AND meta LIKE '%\"resolved\"%'"
                         " AND json_extract(meta,'$.resolved')=1", (t, m["id"], m["agent_id"])).fetchone() or \
            c.execute("SELECT 1 FROM thread_completions WHERE thread_id=? AND through_id>=?", (t, m["id"])).fetchone()
    q = ("SELECT m.id, m.thread_id, m.agent_id, m.body, m.meta, m.created_at, a.handle author, b.slug channel"
         " FROM messages m JOIN agents a ON a.id=m.agent_id JOIN boards b ON b.id=m.board_id")
    waiting, seen = [], set()
    marked = c.execute(q + " WHERE m.agent_id<>? AND (m.meta LIKE '%\"needs\"%' OR m.id IN"
                       " (SELECT message_id FROM tags WHERE tag='needs-human')) ORDER BY m.id DESC LIMIT 500", (me,))
    for m in marked:
        meta = json.loads(m["meta"] or "{}")
        tagged = c.execute("SELECT 1 FROM tags WHERE message_id=? AND tag='needs-human'", (m["id"],)).fetchone() and \
            c.execute("SELECT 1 FROM mentions WHERE message_id=? AND agent_id=?", (m["id"], me)).fetchone()
        if not (_needs(meta, handle) or tagged) or answered(m) or resolved(m):
            continue
        seen.add(m["id"])
        if f"m{m['id']}" in gone:
            continue
        ask = str(meta.get("ask") or "").strip() or m["body"].split("\n", 1)[0]
        waiting.append({"key": f"m{m['id']}", "where": "@" + m["author"], "text": ask[:200],
                        "href": f"/t/{m['thread_id'] or m['id']}", "at": m["created_at"]})
    mentions = []
    rows = c.execute(q + " JOIN mentions x ON x.message_id=m.id WHERE x.agent_id=? AND m.agent_id<>?"
                     " AND m.created_at>=? ORDER BY m.id DESC LIMIT 200", (me, me, since))
    for m in rows:
        if m["id"] in seen or f"m{m['id']}" in gone or answered(m):
            continue
        mentions.append({"key": f"m{m['id']}", "where": "@" + m["author"], "text": m["body"].split("\n", 1)[0][:200],
                         "href": f"/t/{m['thread_id'] or m['id']}", "at": m["created_at"]})
        if len(mentions) >= 30:
            break
    return {"waiting": waiting, "mentions": mentions}


def _versions(hosts: list, attention: list) -> list:
    """What runs against what should (#239); only what a person must fix goes on the bell."""
    from . import versions
    try:
        from . import routines
        recipes = routines.list_routines(full=False)
    except Exception:       # a board without routines, or bin/routines missing
        recipes = []
    rows = versions.rows(hosts, recipes)
    for text, href in versions.attention(rows):
        attention.append(_item(text, href, "fleet"))
    return rows


def rollup(hosts: list | None = None, agent: dict | None = None) -> dict:
    """{ok, attention: [{text, href, where, key}], dismissed, fleet, workspaces, routines}.
    hosts: fleet.hosts(), when the caller has it already (each call scans the messages for last posts).
    `ok` and `attention` leave out what a human dismissed; `dismissed` lists those."""
    attention: list = []
    hosts = fleet.hosts() if hosts is None else hosts
    out = {"fleet": _fleet(hosts, attention), "workspaces": _workspaces(hosts, attention),
           "routines": _routines(attention), "versions": _versions(hosts, attention)}
    shown, hidden = _split(attention)
    mine = asks(agent) if agent and agent["kind"] == "human" else {}
    return {"ok": not shown, "attention": shown, "dismissed": hidden, **mine, **out}
