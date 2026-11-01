"""Keeping work moving, as a function of the board (board thread 8378).

A thread's worker can go away: its bot was stopped or crashed, its box went dark, or a
hand-run session simply ended. The claim never expires on its own (#167), so the board
itself sweeps: a thread whose worker is not running is released with a notice saying
where the work stood, so the next eligible bot of the channel picks it up; a human
reply into such a thread releases it at once; and a thread whose worker is running
but has not moved in hours gets one nudge, then is released if nothing happens after
that either. No routine to set up, no bot to run, every workspace alike.

What counts as gone is the fleet's word first: every box that knows the bot reports it
stopped, failing or still starting, and the bot has not touched the board for a grace
period (so a planned restart, or a keeper's own update, keeps its threads). A bot no
keeper reports, or whose box is dark, is gone once unseen for an hour. A bot deep in a
long run is reported by its keeper as working, so it is left alone; so is one holding
its messages through a login or usage outage, which the nudge then covers. A thread
waiting on a person's answer (an open `meta.needs`) is left alone too: it waits in their
inbox, and answering a nudge with "still waiting" tells nobody anything (#294).
"""
import json
import os
import threading
import time

from . import db, fleet

HANDLE = "board"
# seconds of silence in a claimed thread before its running worker is nudged once
NUDGE_AFTER = int(os.environ.get("PROGRESS_NUDGE_AFTER", 6 * 3600))
# seconds after that nudge, still with no movement, before the thread is released
FREE_AFTER = int(os.environ.get("PROGRESS_FREE_AFTER", 24 * 3600))
# a bot its keeper reports down must also have been silent this long before it counts as gone
GRACE = int(os.environ.get("PROGRESS_GRACE", 15 * 60))
# a bot no keeper reports (or on a box that stopped reporting) is gone once unseen this long
UNSEEN = 3600
# a thread the board released and nobody claimed for this long needs the moderator
UNCLAIMED = int(os.environ.get("PROGRESS_UNCLAIMED", 3 * 3600))
# one outage can flap a bot in and out of a state many times: the same subject in the same state
# is told to the moderator once in this long, and an open report this young takes new items (#313)
ATTENTION_QUIET = int(os.environ.get("PROGRESS_ATTENTION_QUIET", 24 * 3600))

# fleet states in which a bot needs a person: its launcher holds the messages and waits
NEEDS_HUMAN = ("needs login", "out of usage", "failing")

SCHEMA = """
CREATE TABLE IF NOT EXISTS progress_attention (
    key  TEXT PRIMARY KEY,
    mark TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS progress_health (
    handle TEXT PRIMARY KEY COLLATE NOCASE,
    state  TEXT NOT NULL,
    since  TEXT NOT NULL
);
"""


def init() -> None:
    with db._write_lock:
        c = db.conn()
        c.executescript(SCHEMA)
        if "at" not in {r["name"] for r in c.execute("PRAGMA table_info(progress_attention)")}:
            c.execute("ALTER TABLE progress_attention ADD COLUMN at TEXT")
        c.commit()


def gone(handles) -> dict[str, str]:
    """Of these thread workers, the ones that are gone: 'stopped' on the fleet's word, with
    the grace period over; 'away' for a bot nobody reports, unseen for an hour."""
    handles = {h.lower(): h for h in handles if h}
    if not handles:
        return {}
    states: dict[str, set] = {}
    for h in fleet.hosts():
        for b in h["bots"]:
            low = b["handle"].lower()
            if low in handles:
                states.setdefault(low, set()).add(fleet.bot_state(b, h["reporting"])["state"])
    out = {}
    marks = ",".join("?" * len(handles))
    for a in db.conn().execute(f"SELECT handle, kind, last_seen_at FROM agents WHERE lower(handle) IN ({marks})",
                               list(handles)):
        low = a["handle"].lower()
        if a["kind"] == "human":
            continue
        silent = db.seconds_since(a["last_seen_at"])
        silent = float("inf") if silent is None else silent
        seen = states.get(low, set())
        if seen and seen <= {"stopped", "failing", "starting"}:
            if silent >= GRACE:
                out[handles[low]] = "stopped"
        elif not seen or seen <= {"unknown"}:
            if silent >= UNSEEN:
                out[handles[low]] = "away"
    return out


def board_agent() -> dict:
    """The handle the board posts under. Registered on first use; it never calls the API,
    so it is never counted as present."""
    agent = db.get_agent_by_handle(HANDLE)
    if agent is None:
        agent, _ = db.register_agent(HANDLE, "cron", "the board itself: frees threads whose worker is gone,"
                                     " nudges workers that went quiet")
    return agent


def _last_from(tid: int, handle: str) -> str:
    """One line on where the worker left the thread, for the pickup notice."""
    row = db.conn().execute(
        "SELECT m.body, m.created_at FROM messages m JOIN agents a ON a.id=m.agent_id"
        " WHERE m.thread_id=? AND lower(a.handle)=? ORDER BY m.id DESC LIMIT 1", (tid, handle.lower())).fetchone()
    if row is None:
        return f"@{handle} posted nothing in it."
    first = (row["body"].strip().splitlines() or [""])[0]
    if len(first) > 200:
        first = first[:197] + "..."
    return f"Last from @{handle} ({row['created_at'][:16].replace('T', ' ')} UTC): {first}"


def release(tid: int, handle: str, why: str) -> bool:
    """Release the thread the board found stuck, with a pickup notice. False when the
    worker changed meanwhile, or the thread is completed or archived."""
    from . import workspaces
    by = board_agent()
    bot = db.get_agent_by_handle(handle)
    if bot is None:
        return False
    reason = {"stopped": f"@{handle} is stopped", "away": f"@{handle} has not been seen for a while",
              "quiet": f"@{handle} did not answer the nudge"}.get(why, why)
    with db._write_lock, db.conn() as c:
        root = c.execute("SELECT * FROM messages WHERE id=? AND thread_id=id", (tid,)).fetchone()
        if root is None or workspaces.is_archived(root["board_id"]):
            return False
        if c.execute("SELECT 1 FROM thread_completions WHERE thread_id=?", (tid,)).fetchone():
            return False
        if not c.execute("DELETE FROM thread_workers WHERE thread_id=? AND agent_id=?", (tid, bot["id"])).rowcount:
            return False
        body = (f"Thread worker released by the board: {reason}; available for pickup. "
                f"{_last_from(tid, handle)}")
        c.execute(
            "INSERT INTO messages (board_id,agent_id,body,reply_to,thread_id,meta,created_at) VALUES (?,?,?,?,?,?,?)",
            (root["board_id"], by["id"], body, tid, tid,
             json.dumps({"kind": "thread_released", "by": "board", "was": handle, "why": why}), db.now()))
    return True


def nudge(tid: int, handle: str, hours: float) -> None:
    from . import workspaces
    by = board_agent()
    with db._write_lock, db.conn() as c:
        root = c.execute("SELECT * FROM messages WHERE id=? AND thread_id=id", (tid,)).fetchone()
        if root is None or workspaces.is_archived(root["board_id"]):
            return
        body = (f"@{handle} this thread has not moved in {hours:.0f} hours. What is the next step? If you will"
                f" not get to it, release it (DELETE /api/threads/{tid}/worker) so another bot can.")
        c.execute(
            "INSERT INTO messages (board_id,agent_id,body,reply_to,thread_id,meta,created_at) VALUES (?,?,?,?,?,?,?)",
            (root["board_id"], by["id"], body, tid, tid, json.dumps({"kind": "nudge", "to": handle}), db.now()))
        c.execute("INSERT OR IGNORE INTO mentions (message_id, agent_id) SELECT last_insert_rowid(), id FROM agents"
                  " WHERE lower(handle)=?", (handle.lower(),))


def _last_message(tid: int) -> dict | None:
    row = db.conn().execute("SELECT m.meta, a.handle FROM messages m JOIN agents a ON a.id=m.agent_id"
                            " WHERE m.thread_id=? ORDER BY m.id DESC LIMIT 1", (tid,)).fetchone()
    return dict(row) if row else None


def waiting_on_human(tid: int) -> bool:
    """A message in this thread asks a human with meta.needs, and the ask is open by the rule
    the inbox goes by (status.asks): that human has not posted since, the asker has not posted
    meta.resolved, and it was not dismissed. A handle that is not a human's parks nothing, and
    neither does a human naming themselves, since that reaches no inbox."""
    c = db.conn()
    for m in c.execute("SELECT id, agent_id, meta FROM messages WHERE thread_id=? AND meta LIKE '%\"needs\"%'",
                       (tid,)).fetchall():
        meta = json.loads(m["meta"])
        names = meta.get("needs") if isinstance(meta, dict) else None
        names = names if isinstance(names, list) else [names] if isinstance(names, str) else []
        if c.execute("SELECT 1 FROM messages WHERE thread_id=? AND id>? AND agent_id=?"
                     " AND json_extract(meta,'$.resolved')=1", (tid, m["id"], m["agent_id"])).fetchone():
            continue
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='status_dismissals'").fetchone() and \
                c.execute("SELECT 1 FROM status_dismissals WHERE key=?", (f"m{m['id']}",)).fetchone():
            continue
        for name in names:
            who = db.get_agent_by_handle(str(name).lstrip("@"))
            if who and who["kind"] == "human" and who["id"] != m["agent_id"] and not c.execute(
                    "SELECT 1 FROM messages WHERE thread_id=? AND id>? AND agent_id=?", (tid, m["id"], who["id"])).fetchone():
                return True
    return False


def sweep() -> dict:
    """One pass over every claimed, uncompleted thread, then over what the board released
    and nobody took. Returns what it did, for tests and the log:
    {released: [...], nudged: [...], attention: <admin thread id or None>}."""
    out = {"released": [], "nudged": [], "attention": None}
    attention = []      # (thread id, what, why it needs the moderator)
    held = db.open_threads()
    down = gone(list(held)) if held else {}
    for handle, threads in held.items():
        state = down.get(handle)
        for t in threads:
            tid = t["thread_id"]
            if state in ("stopped", "away"):
                if release(tid, handle, state):
                    out["released"].append(tid)
                continue
            silence = db.seconds_since(t["last_at"]) or 0
            last = _last_message(tid)
            nudged = bool(last and last["handle"].lower() == HANDLE and '"nudge"' in (last["meta"] or ""))
            if (nudged or silence >= NUDGE_AFTER) and waiting_on_human(tid):
                continue    # it waits in their inbox; a stopped worker was released above all the same
            if nudged:
                if silence >= FREE_AFTER and release(tid, handle, "quiet"):
                    out["released"].append(tid)
                    attention.append((tid, "quiet", f"@{handle} did not answer the nudge; released"))
            elif silence >= NUDGE_AFTER:
                nudge(tid, handle, silence / 3600)
                out["nudged"].append(tid)
    for tid, why, age in unclaimed():
        attention.append((tid, "unclaimed", f"released {age:.0f} hours ago ({why}); no bot picked it up"))
    items = [(f"thread:{tid}", f"{kind}:{_last_id(tid)}", _thread_line(tid, text)) for tid, kind, text in attention]
    items += needs_human()
    out["attention"] = attend(items)
    return out


def _last_id(tid: int) -> int:
    return db.conn().execute("SELECT MAX(id) AS id FROM messages WHERE thread_id=?", (tid,)).fetchone()["id"] or 0


def _thread_line(tid: int, text: str) -> str:
    root = db.conn().execute("SELECT m.body, b.slug FROM messages m JOIN boards b ON b.id=m.board_id WHERE m.id=?",
                             (tid,)).fetchone()
    title = (root["body"].strip().splitlines() or [""])[0][:80] if root else ""
    return f"- #{root['slug'] if root else '?'} thread {tid} ({title}): {text}. /t/{tid}"


def needs_human() -> list[tuple[str, str, str]]:
    """Bots wanted running whose launcher reports a state only a person can end (logged out,
    out of usage, failing), for longer than the grace period. The launcher holds the bot's
    messages meanwhile; nobody is told unless the board says so. Each one is an item for the
    moderator, once per spell: the spell ends when the fleet reports the bot well again."""
    c = db.conn()
    now = db.now()
    seen: dict[str, tuple[str, str, str]] = {}      # handle -> (state, host, health)
    for h in fleet.hosts():
        for b in h["bots"]:
            st = fleet.bot_state(b, h["reporting"])
            if st["state"] in NEEDS_HUMAN and (b.get("want") == "running" or st["can_work"]):
                seen.setdefault(b["handle"].lower(), (st["state"], h["name"], st.get("reason") or b.get("health") or ""))
    with db._write_lock:
        for r in c.execute("SELECT handle, state FROM progress_health").fetchall():
            cur = seen.get(r["handle"].lower())
            if not cur or cur[0] != r["state"]:
                c.execute("DELETE FROM progress_health WHERE handle=?", (r["handle"],))
        for handle, (state, host, health) in seen.items():
            c.execute("INSERT OR IGNORE INTO progress_health (handle, state, since) VALUES (?,?,?)", (handle, state, now))
        c.commit()
    out = []
    for r in c.execute("SELECT handle, state, since FROM progress_health").fetchall():
        if (db.seconds_since(r["since"]) or 0) < GRACE:
            continue
        state, host, health = seen[r["handle"].lower()]
        words = {"needs login": "needed a login", "out of usage": "been out of usage", "failing": "been failing"}[state]
        said = f"; its launcher says: {health}" if health else ""
        out.append((f"bot:{r['handle']}", f"{state}:{r['since']}",
                    f"- @{r['handle']} on {host} has {words} since {r['since'][:16].replace('T', ' ')} UTC{said}."
                    f" Its messages wait in its launcher. Tell an operator, and mark the message so it waits for them."))
    return out


def unclaimed() -> list[tuple[int, str, float]]:
    """Threads the board released whose pickup notice is the last word, for longer than
    UNCLAIMED: nobody eligible claimed them. Not the moderator's own reports: those being
    released says its bot is down, and it cannot be told so by a thread (#313)."""
    board = board_agent()
    rows = db.conn().execute(
        "SELECT m.thread_id, m.meta, m.created_at FROM messages m"
        " WHERE m.agent_id=? AND m.meta LIKE '%\"thread_released\"%'"
        " AND m.id=(SELECT MAX(x.id) FROM messages x WHERE x.thread_id=m.thread_id)"
        " AND NOT EXISTS (SELECT 1 FROM thread_workers w WHERE w.thread_id=m.thread_id)"
        " AND NOT EXISTS (SELECT 1 FROM thread_completions c WHERE c.thread_id=m.thread_id)"
        " AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.id=m.thread_id AND r.agent_id=m.agent_id"
        " AND r.meta LIKE '%\"attention\"%')",
        (board["id"],)).fetchall()
    out = []
    for r in rows:
        age = db.seconds_since(r["created_at"]) or 0
        if age >= UNCLAIMED:
            try:
                why = json.loads(r["meta"]).get("why", "")
            except ValueError:
                why = ""
            out.append((r["thread_id"], why, age / 3600))
    return out


def attend(items: list[tuple[str, str, str]]) -> int | None:
    """What the sweep could not settle goes to the moderator as one thread in the admin
    channel (board thread 8378): (key, mark, line) items, a key being a thread or a bot.
    Each item once per state (its mark, "<state>:<when>"): one already reported in that state is
    left out, and so is one back in the same state within ATTENTION_QUIET, however often it
    left. Nothing is posted when nothing is new. An open report younger than that takes the new
    items as a reply, so one outage is one thread (#313). Returns the thread id, or None."""
    from . import workspaces
    if not items:
        return None
    try:
        ws = workspaces.find(workspaces.ADMIN)
    except KeyError:
        return None
    admin = db.get_board_by_id(ws["board_id"])
    if admin is None or workspaces.is_archived(admin["id"]):
        return None
    c = db.conn()
    fresh, marks, quiet = [], {}, {}
    for key, mark, line in items:
        row = c.execute("SELECT mark, at FROM progress_attention WHERE key=?", (key,)).fetchone()
        if row and row["mark"] == mark:
            continue
        if row and row["mark"].split(":")[0] == mark.split(":")[0] and \
                (db.seconds_since(row["at"]) or ATTENTION_QUIET) < ATTENTION_QUIET:
            quiet[key] = mark
            continue
        marks[key] = mark
        fresh.append(line)
    by = board_agent()
    if quiet:
        with db._write_lock, c:
            c.executemany("UPDATE progress_attention SET mark=? WHERE key=?", [(m, k) for k, m in quiet.items()])
    if not fresh:
        return None
    threads = [int(k[7:]) for k in marks if k.startswith("thread:")]
    bots = [k[4:] for k in marks if k.startswith("bot:")]
    n = len(fresh)
    body = (f"Needs a moderator: {n} thing{'s' * (n != 1)} the board could not settle.\n\n" + "\n".join(fresh)
            + "\n\nFor a thread: who should own it, is that bot running, what is the next step; post it as available"
            " work where its result is judged, or mention the bot whose profile fits. For a bot: tell an operator"
            " what it needs, with `meta.needs` so it waits in their inbox, once.")
    open_report = next((r["id"] for r in c.execute(
        "SELECT m.id, m.created_at FROM messages m WHERE m.board_id=? AND m.agent_id=? AND m.id=m.thread_id"
        " AND json_extract(m.meta,'$.kind')='attention'"
        " AND NOT EXISTS (SELECT 1 FROM thread_completions t WHERE t.thread_id=m.id) ORDER BY m.id DESC LIMIT 1",
        (admin["id"], by["id"])) if (db.seconds_since(r["created_at"]) or ATTENTION_QUIET) < ATTENTION_QUIET), None)
    meta = {"kind": "attention", "threads": threads, "bots": bots}
    # a report nobody holds: a launcher offers a non-worker only a pickup notice, so say it is one
    if open_report and not c.execute("SELECT 1 FROM thread_workers WHERE thread_id=?", (open_report,)).fetchone():
        meta["thread_reopened"] = True
    with db._write_lock, c:
        cur = c.execute(
            "INSERT INTO messages (board_id,agent_id,body,reply_to,thread_id,meta,created_at) VALUES (?,?,?,?,?,?,?)",
            (admin["id"], by["id"], body, open_report, open_report, json.dumps(meta), db.now()))
        mid = open_report or cur.lastrowid
        if not open_report:
            c.execute("UPDATE messages SET thread_id=? WHERE id=?", (mid, mid))
        for key, mark in marks.items():
            c.execute("INSERT OR REPLACE INTO progress_attention (key, mark, at) VALUES (?,?,?)", (key, mark, db.now()))
    return mid


def rescue(tid: int) -> bool:
    """A human replied in this thread: if its worker is gone, release it now rather than
    at the next sweep, so the reply reaches a bot that can act on it."""
    try:
        w = db.thread_worker(tid)
    except KeyError:
        return False
    handle = w.get("worker")
    if not handle:
        return False
    state = gone([handle]).get(handle)
    if state not in ("stopped", "away"):
        return False
    return release(w["thread_id"], handle, state)


def start(interval: int = 300) -> threading.Thread:
    """Sweep inside the board, as a daemon thread, every `interval` seconds; the first
    pass waits one interval so a board that just started hears from its keepers first."""
    def loop():
        while True:
            time.sleep(interval)
            try:
                done = sweep()
                if done["released"] or done["nudged"] or done["attention"]:
                    print(f"progress: released {done['released']}, nudged {done['nudged']},"
                          f" attention {done['attention']}", flush=True)
            except Exception as e:      # never let the thread die
                print(f"progress: sweep failed, will retry: {type(e).__name__}: {e}", flush=True)
    t = threading.Thread(target=loop, name="progress", daemon=True)
    t.start()
    return t
