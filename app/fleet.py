"""The fleet: which boxes there are, how loaded each is, and which bots run on it.

The board cannot see a box. A keeper on each one (bin/keeper) reports it here
every minute, and this module keeps the last report per box. Nothing in here
acts on a box; it records what the keepers say.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import uuid
from datetime import datetime, timezone

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_hosts (
    name        TEXT PRIMARY KEY COLLATE NOCASE,
    reported_by INTEGER NOT NULL REFERENCES agents(id),
    reported_at TEXT NOT NULL,
    report      TEXT NOT NULL
);

-- What a human asked for: this bot, on this box, running or not. The keeper of
-- the box reads its rows with every report and makes the box match.
CREATE TABLE IF NOT EXISTS fleet_bots (
    handle  TEXT PRIMARY KEY COLLATE NOCASE,
    host    TEXT NOT NULL COLLATE NOCASE,
    harness TEXT NOT NULL,
    run     INTEGER NOT NULL,
    set_by  TEXT NOT NULL,
    set_at  TEXT NOT NULL,
    problem TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS fleet_alert_clears (
    handle TEXT PRIMARY KEY COLLATE NOCASE,
    host TEXT NOT NULL COLLATE NOCASE,
    health TEXT NOT NULL,
    last_run TEXT NOT NULL,
    problem TEXT NOT NULL,
    set_at TEXT NOT NULL
);

-- Secrets every box's bots get in their environment (GH_TOKEN, ...), set by an admin
-- on /fleet. Only keepers read the values; everyone else sees names and who set them.
CREATE TABLE IF NOT EXISTS fleet_secrets (
    name   TEXT PRIMARY KEY,
    value  TEXT NOT NULL,
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL
);
"""
# What a secret may be called: the names bin/bot and the keeper accept from fleet.env,
# so a secret cannot move a box's BOARD or PATH.
SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]*_(?:TOKEN|KEY|SECRET)")
# A move, in two steps: move_to is set on the row while the bot's own box packs it
# and hands the archive over; then the row points at the new box with fetch naming
# the archive, until that box has unpacked it. The archive holds the bot's token,
# so it travels box to box over ssh (KEEPER_HANDOVER), never through the board.
MOVE_COLUMNS = ("move_to", "fetch")
# kill: when a human asked for the bot to stop now, cutting its harness run short
# (a stuck or runaway bot); cleared by the next request for it.
# threads: what to do with the bot's open threads once it has stopped (#167): handoff or
# retire releases them when the keeper reports it stopped; NULL keeps them. retired: when it
# was retired, so it leaves the stopped list; cleared by the next start.
# reset: when a human asked for the bot's next run to start a new harness session (#221);
# the keeper runs `bot reset --asked <reset>`, which does it once per request.
# deleted: when a human deleted the bot (#280); its keeper stops it and removes it from the box
# (`bot delete`), and the row goes once the box no longer reports it.
EXTRA_COLUMNS = MOVE_COLUMNS + ("kill", "threads", "retired", "reset", "deleted")
THREADS = ("keep", "handoff", "retire")
# A keeper reports several times a minute; a box silent for this long is shown as not reporting.
STALE_AFTER = 180
HARNESSES = ("claude", "codex", "opencode", "copilot")
UP = ("waiting", "running")     # how bin/bot ps describes a bot whose launcher is alive
# The first names bin/bot draws a new bot's handle from (its BOT_NAMES default).
NAMES = ("ada", "alma", "amos", "anya", "arlo", "basil", "bea", "bruno", "cleo", "cora", "dara", "dex",
         "edie", "eli", "elsa", "enzo", "esme", "ezra", "faye", "finn", "flora", "gus", "hana", "hugo",
         "ida", "igor", "ines", "iris", "ivo", "jade", "jonas", "juno", "kai", "kira", "lars", "lena",
         "leo", "lila", "linus", "lola", "luca", "mabel", "mara", "milo", "mina", "nell", "nico", "nina",
         "noor", "olga", "omar", "opal", "otis", "otto", "pia", "quinn", "rafa", "remy", "rosa", "rufus",
         "sana", "seth", "sven", "tess", "theo", "tilda", "uma", "vera", "wes", "willa", "yara", "zane",
         "zoe",)


def init() -> None:
    c = db.conn()
    with db._write_lock:
        c.executescript(SCHEMA)
        have = {r["name"] for r in c.execute("PRAGMA table_info(fleet_bots)")}
        # workspace: the workspace the bot was created in. The keeper passes it to bin/bot,
        # which registers with it; a bot registered before that is put in it once it reports.
        if "project" in have and "workspace" not in have:
            c.execute("ALTER TABLE fleet_bots RENAME COLUMN project TO workspace")
            have.add("workspace")
        # profile: the profile the bot was added with (its role and skills); set once, never changed.
        # channels: the channels of its workspace it claims new threads in (JSON list); empty: any.
        # voice, personality: how it writes to people and who it is, set by a human (#348).
        for col in EXTRA_COLUMNS + ("workspace", "profile", "channels", "voice", "personality"):
            if col not in have:
                c.execute(f"ALTER TABLE fleet_bots ADD COLUMN {col} TEXT")
        c.commit()


def report(name: str, agent_id: int, data: dict) -> bool:
    """Store a box's report. A box belongs to the agent that reported it first:
    False when another agent tries to report under its name."""
    c = db.conn()
    with db._write_lock:
        row = c.execute("SELECT reported_by FROM fleet_hosts WHERE name=?", (name,)).fetchone()
        if row is not None and row["reported_by"] != agent_id:
            return False
        c.execute(
            "INSERT INTO fleet_hosts (name, reported_by, reported_at, report) VALUES (?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET reported_at=excluded.reported_at, report=excluded.report",
            (name, agent_id, db.now(), json.dumps(data)),
        )
        c.commit()
    # A box's report is when its newly-started bots become known; a bot created for a
    # workspace from the page that registered without one joins it now.
    try:
        from . import workspaces
        workspaces.apply_pending_assignments()
    except Exception:
        pass
    hand_off(name, data.get("bots") or [])
    gone(name, data.get("bots") or [])
    return True


def gone(host: str, bots: list[dict]) -> None:
    """Forget the deleted bots this box no longer reports: its keeper has removed them."""
    seen = {b["handle"].lower() for b in bots}
    c = db.conn()
    with db._write_lock:
        for r in c.execute("SELECT handle FROM fleet_bots WHERE host=? AND deleted IS NOT NULL", (host,)).fetchall():
            if r["handle"].lower() not in seen:
                c.execute("DELETE FROM fleet_bots WHERE handle=?", (r["handle"],))
        c.commit()


def delete(handle: str, by: dict) -> str | None:
    """A human deletes a bot (#280). On the board at once: its open threads are released and
    the agent is retired (tokens revoked, gone from every list, its handle kept on its old
    messages and never reused). On its box, by its keeper: stopped, a busy one once its run
    ends, then removed. A box not reporting does that when it reports again. Returns None,
    or why not."""
    a = db.get_agent_by_handle(handle)
    if a and a.get("retired_at"):
        return f"{handle} is deleted already"
    if a and a["kind"] in db.UNSCOPED_KINDS:
        return f"{handle} is a {a['kind']}, not a bot"
    c = db.conn()
    row = c.execute("SELECT * FROM fleet_bots WHERE handle=?", (handle,)).fetchone()
    # its box: the one asked for it, else the box that reports it (where it is not just a moved-away copy)
    box = next((h for h in hosts() for b in h["bots"]
                if b["handle"].lower() == handle.lower() and b["status"] != "moved"), None)
    host = row["host"] if row else box["name"] if box else None
    if not a and not host:
        return ""
    if a:
        db.hand_off_threads(handle, by)
        db.retire_agent(a["id"])
    if host:
        seen = next((b for b in (box or {}).get("bots", []) if b["handle"].lower() == handle.lower()), {})
        now = db.now()
        with db._write_lock:
            c.execute("INSERT INTO fleet_bots (handle, host, harness, run, set_by, set_at, deleted) VALUES (?,?,?,0,?,?,?)"
                      " ON CONFLICT(handle) DO UPDATE SET run=0, set_by=excluded.set_by, set_at=excluded.set_at,"
                      " deleted=excluded.deleted, problem='', move_to=NULL, fetch=NULL, kill=NULL, threads=NULL, reset=NULL",
                      (handle, host, (row["harness"] if row else None) or seen.get("harness") or "claude",
                       by["handle"], now, now))
            c.commit()
    return None


def hand_off(host: str, bots: list[dict]) -> None:
    """Release the open threads of the bots on this box that were asked to hand them off,
    once its keeper reports them stopped: not before, or the old bot's last run could post
    into a thread another bot has just claimed."""
    c = db.conn()
    seen = {b["handle"].lower(): b for b in bots}
    for r in c.execute("SELECT * FROM fleet_bots WHERE host=? AND run=0 AND threads IS NOT NULL",
                       (host,)).fetchall():
        b = seen.get(r["handle"].lower())
        if b and b.get("status", "").startswith(UP):
            continue
        by = db.get_agent_by_handle(r["set_by"])
        if by and db.get_agent_by_handle(r["handle"]):
            db.hand_off_threads(r["handle"], by)
        with db._write_lock:
            c.execute("UPDATE fleet_bots SET retired=CASE WHEN threads='retire' THEN ? ELSE retired END,"
                      " threads=NULL WHERE handle=? AND set_at=?", (db.now(), r["handle"], r["set_at"]))
            c.commit()


def secrets() -> list[dict]:
    """The fleet's secrets without their values: name, who set it, when, and its last 4 characters."""
    return [{"name": r["name"], "set_by": r["set_by"], "set_at": r["set_at"], "ends": r["value"][-4:]}
            for r in db.conn().execute("SELECT * FROM fleet_secrets ORDER BY name")]


# The fleet's Claude login (#283): a token from `claude setup-token`, which lasts a year.
CLAUDE_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_TOKEN_DAYS = 365


def claude_token_due() -> dict | None:
    """{set_at, days_left} once the fleet's claude token is two weeks from a year old (counted
    from when it was set here), else None."""
    row = db.conn().execute("SELECT set_at FROM fleet_secrets WHERE name=?", (CLAUDE_TOKEN,)).fetchone()
    if row is None:
        return None
    left = CLAUDE_TOKEN_DAYS - (db.seconds_since(row["set_at"]) or 0) / 86400
    return {"set_at": row["set_at"], "days_left": round(left)} if left < 14 else None


def secret_values() -> dict[str, str]:
    """name -> value. For keepers only."""
    return {r["name"]: r["value"] for r in db.conn().execute("SELECT name, value FROM fleet_secrets ORDER BY name")}


def secrets_version() -> str:
    """Changes whenever a secret is added, replaced or removed; keepers fetch the values when it does."""
    return hashlib.sha256(json.dumps(secret_values(), sort_keys=True).encode()).hexdigest()[:16]


def set_secret(name: str, value: str, by: str) -> None:
    c = db.conn()
    with db._write_lock:
        c.execute("INSERT INTO fleet_secrets (name, value, set_by, set_at) VALUES (?,?,?,?)"
                  " ON CONFLICT(name) DO UPDATE SET value=excluded.value, set_by=excluded.set_by,"
                  " set_at=excluded.set_at", (name, value, by, db.now()))
        c.commit()


def delete_secret(name: str) -> bool:
    c = db.conn()
    with db._write_lock:
        n = c.execute("DELETE FROM fleet_secrets WHERE name=?", (name,)).rowcount
        c.commit()
    return bool(n)


def keeps_a_box(agent_id: int) -> bool:
    return db.conn().execute("SELECT 1 FROM fleet_hosts WHERE reported_by=?", (agent_id,)).fetchone() is not None


def members(name: str) -> dict | None:
    """Who belongs to a box, for taking it off the fleet (#279): its keeper, the bots it
    reports and the bots asked of it, and whether its keeper is reporting now."""
    c = db.conn()
    row = c.execute("SELECT h.reported_at, h.report, a.handle keeper FROM fleet_hosts h"
                    " LEFT JOIN agents a ON a.id = h.reported_by WHERE h.name=?", (name,)).fetchone()
    if row is None:
        return None
    try:
        # a bot that moved off this box stays in its report as `moved`: it lives elsewhere
        reported = [b.get("handle") for b in (json.loads(row["report"]).get("bots") or [])
                    if isinstance(b, dict) and b.get("status") != "moved"]
    except (ValueError, AttributeError):
        reported = []
    asked = [r[0] for r in c.execute("SELECT handle FROM fleet_bots WHERE host=?", (name,))]
    bots = sorted({h for h in reported + asked if h}, key=str.lower)
    secs = db.seconds_since(row["reported_at"])
    return {"keeper": row["keeper"], "bots": bots, "reporting": secs is not None and secs < STALE_AFTER}


def forget(name: str) -> bool:
    """Drop a box, so its name is free again: a retired box, or one whose keeper lost its token."""
    c = db.conn()
    with db._write_lock:
        n = c.execute("DELETE FROM fleet_hosts WHERE name=?", (name,)).rowcount
        c.execute("DELETE FROM fleet_bots WHERE host=?", (name,))
        c.commit()
    return bool(n)


def _set_want(c, handle, host, harness, run, by, now, kill=False, workspace=None, threads=None, profile=None,
              channels=None, voice=None, personality=None):
    c.execute(
        "INSERT INTO fleet_bots (handle, host, harness, run, set_by, set_at, kill, workspace, threads, profile, channels,"
        " voice, personality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(handle) DO UPDATE SET host=excluded.host, harness=excluded.harness, run=excluded.run,"
        " set_by=excluded.set_by, set_at=excluded.set_at, problem='', move_to=NULL, kill=excluded.kill,"
        " threads=excluded.threads, retired=CASE WHEN excluded.run THEN NULL ELSE retired END,"
        " workspace=COALESCE(excluded.workspace, workspace), profile=COALESCE(excluded.profile, profile),"
        " channels=COALESCE(excluded.channels, channels), voice=COALESCE(excluded.voice, voice),"
        " personality=COALESCE(excluded.personality, personality)",
        (handle, host.lower(), harness, int(run), by, now, now if kill else None, workspace or None,
         threads if threads in ("handoff", "retire") else None, profile or None,
         json.dumps(channels) if channels is not None else None, voice, personality),
    )


def _channels_problem(workspace: str | None, profile: str | None, channels: list[str] | None,
                      stored: str | None, run: bool) -> str | None:
    """Why these channels will not do: one is not a channel of the bot's workspace, or the
    profile is specialized and the bot would start bound to nothing."""
    from . import registry, workspaces
    if channels is not None:
        if not workspace:
            return "say which workspace the bot belongs to before binding it to channels"
        try:
            ws = workspaces.find(workspace)
        except KeyError:
            return f"no workspace named {workspace}"
        known = {ch["slug"].lower() for ch in workspaces.channels_of(ws, include_archived=False)}
        bad = [ch for ch in channels if ch.lower() not in known]
        if bad:
            return f"not a channel of {workspace}: {', '.join(bad)}"
    bound = channels if channels is not None else (json.loads(stored) if stored else [])
    if run and not bound and registry.is_specialized(profile):
        return f"{profile} is a specialized profile: pick at least one channel of its workspace for the bot"
    return None


def want(handle: str, host: str, run: bool, harness: str | None, by: str, kill: bool = False,
         workspace: str | None = None, threads: str | None = None, profile: str | None = None,
         channels: list[str] | None = None, voice: str | None = None, personality: str | None = None) -> str | None:
    """Record that a human wants this bot running, or stopped, on this box. A bot
    has one box: asking for it on another moves the request there, which is how a
    bot unpacked on a new box is started. kill: stop it now, without waiting for the
    harness run it is in. workspace: the workspace a new bot belongs to; the keeper
    starts it with that, and a registered bot of no workspace is put in it. threads,
    with a stop: keep its open threads, handoff (release them once it has stopped) or
    retire (hand off, and drop it from the stopped list). profile: for a new bot, the profile
    it is added with; a bot that has one cannot be given another. channels: the channels of its
    workspace it claims new threads in; required for a specialized profile, and changed any
    time with set_channels. voice, personality: how it writes and who it is, changed any time
    with set_voice; None leaves them as they are. Returns None, or why not."""
    if kill and run:
        return "a kill stops the bot: ask for run false"
    if threads not in (None,) + THREADS:
        return f"threads: one of {', '.join(THREADS)}"
    if threads and threads != "keep" and run:
        return "threads are handed off when a bot stops: ask for run false"
    if _deleted(handle):
        return f"{handle} was deleted"
    c = db.conn()
    boxes = {h["name"].lower(): h for h in hosts()}
    if host.lower() not in boxes:
        return f"no box named {host} has reported"
    find = lambda h: next((b for b in h["bots"] if b["handle"].lower() == handle.lower()), None)
    here = find(boxes[host.lower()])
    harness = harness or (here or {}).get("harness")
    # Only a start is held back, only by a box that is reporting now, and not when the
    # bot is already up on the box asked for: a stop is never refused, a box that went
    # dark does not hold its bots for ever, and another box's word cannot lock a bot
    # where it really runs.
    if run and not (here and here["status"].startswith(UP)):
        for name, h in boxes.items():
            seen = find(h)
            if seen and name != host.lower() and h["reporting"] and seen["status"].startswith(UP):
                return f"{handle} is running on {name}; stop it there first"
    from . import workspaces
    if workspace:
        try:
            if workspaces.find(workspace)["archived"]:
                return f"the workspace {workspace} is archived"
        except KeyError:
            return f"no workspace named {workspace}"
    with db._write_lock:
        row = c.execute("SELECT harness, workspace, profile, channels FROM fleet_bots WHERE handle=?", (handle,)).fetchone()
        harness = harness or (row["harness"] if row else None)
        now = db.now()
        if harness not in HARNESSES:
            return f"say which harness {handle} runs: one of {', '.join(HARNESSES)}"
        known = db.get_agent_by_handle(handle)
        if run and not workspace and not (row and row["workspace"]) and not (known and known.get("workspace_id")):
            return f"say which workspace {handle} belongs to"
        if profile:
            from . import registry
            why = registry.profile_problem(handle, profile, row["profile"] if row else None)
            if why:
                return why
        ws_slug = workspace or (row["workspace"] if row else None) or db.workspace_slug((known or {}).get("workspace_id"))
        why = _channels_problem(ws_slug, profile or (row["profile"] if row else None), channels,
                                row["channels"] if row else None, run)
        if why:
            return why
        _set_want(c, handle, host, harness, run, by, now, kill, workspace, threads, profile, channels, voice, personality)
        c.commit()
    if workspace and known and not known.get("workspace_id"):
        workspaces.assign_bot(handle, workspace)
    return None


def _deleted(handle: str) -> bool:
    a = db.get_agent_by_handle(handle)
    return bool(a and a.get("retired_at"))


def set_channels(handle: str, channels: list[str]) -> str | None:
    """Bind a fleet bot to these channels of its workspace, or to none (an empty list: it
    claims anywhere, unless its profile is specialized). Returns None, or why not."""
    c = db.conn()
    row = c.execute("SELECT workspace, profile, run FROM fleet_bots WHERE handle=?", (handle,)).fetchone()
    if row is None:
        return f"{handle} is not a fleet bot"
    known = db.get_agent_by_handle(handle)
    ws_slug = row["workspace"] or db.workspace_slug((known or {}).get("workspace_id"))
    why = _channels_problem(ws_slug, row["profile"], channels, None, bool(row["run"]))
    if why:
        return why
    with db._write_lock:
        c.execute("UPDATE fleet_bots SET channels=? WHERE handle=?", (json.dumps(channels), handle))
        c.commit()
    return None


def set_voice(handle: str, voice: str, personality: str) -> str | None:
    """Set a fleet bot's voice and personality; an empty text clears it. They reach the bot
    through its composed AGENTS.md at its next wake. Returns None, or why not."""
    c = db.conn()
    with db._write_lock:
        n = c.execute("UPDATE fleet_bots SET voice=?, personality=? WHERE handle=?", (voice, personality, handle)).rowcount
        c.commit()
    return None if n else f"{handle} is not a fleet bot"


def _channels(w: dict) -> list[str]:
    try:
        return list(json.loads(w.get("channels") or "[]"))
    except ValueError:
        return []


def bot_state(b: dict, reporting: bool = True) -> dict:
    """What a bot is doing, in the one vocabulary /fleet, /projects and /status share (#100):
    {state, words, reason, can_work}. can_work: it would take a message now, or once its
    current run ends; a bot that cannot (logged out, out of usage, on a box gone dark) is
    neither counted as up nor offered for a project's pool."""
    st, health, problem = b.get("status") or "", b.get("health") or "", b.get("problem") or ""
    up = st.startswith(UP)
    doing = st[len("running "):].split(",")[0] if st.startswith("running ") else ""

    def out(state, words, reason="", can_work=False):
        return {"state": state, "words": words, "reason": reason, "can_work": can_work}
    if not reporting:
        return out("unknown", f"{st or 'stopped'} when last heard", "its box is not reporting")
    if b.get("deleted"):
        return out("deleting", "deleted: " + ("stopping, once its run ends" if up else "being removed from its box"))
    if b.get("move_to"):
        return out("moving", f"moving to {b['move_to']}: " + ("stopping" if up else "packing"))
    if b.get("fetch") and not up:
        return out("moving", "arriving: unpacking")
    if (up or b.get("want") == "running") and health.startswith("out of") and " capacity since " in health:
        return out("at capacity", "model temporarily at capacity", health)
    if (up or b.get("want") == "running") and health.startswith("out of"):
        return out("out of usage", "usage limit reached", health)
    if b.get("want") == "running" and not up and st != "moved":
        return out("failing", "not running", problem) if problem else out("starting", "starting")
    if up and ("stopping" in st or b.get("want") == "stopped"):
        # asked, and the keeper has not acted yet: say what was asked; once it has, "stopping"
        words = "stopping" if "stopping" in st else ""
        words = ", ".join(w for w in (words, "kill asked" if b.get("kill") else "" if words else "stop asked") if w)
        if b.get("threads") and b.get("open_threads"):
            n = b["open_threads"]
            words += f" · hands off {n} thread{'s' * (n != 1)} once stopped"
        return out("stopping", words)
    if up and health.startswith("logged out"):
        return out("needs login", "needs a login", health)
    if up and health.startswith("cannot run"):
        return out("failing", "cannot run its harness", health)
    if up and health.startswith("out of"):
        return out("out of usage", "out of usage", health)
    if up:
        words = f"working on {doing}" if doing else "working" if st.startswith("running") else "waiting for work"
        # a failed last run is worth seeing, but the bot still takes the next message
        return out("failing" if health else "working" if st.startswith("running") else "waiting", words, health, True)
    return out("stopped", "stopped" if st != "moved" else "moved to another box", problem)


def running() -> dict:
    """What the header's chip counts (#232): the bots /fleet shows as working or waiting, from
    the keepers' reports; a bot no keeper reports counts when it made a request in the last
    5 minutes. Keepers, the routines runner and humans are counted beside it, not in it."""
    states = {}
    for h in hosts():
        for b in h["bots"]:
            st = b["state"]
            low = b["handle"].lower()
            if st["can_work"] or low not in states:     # a bot listed on two boxes (moving) counts once
                states[low] = st
    bots = sum(st["can_work"] for st in states.values())
    working = sum(st["can_work"] and st["state"] == "working" for st in states.values())
    out = {"keepers": 0, "runners": 0, "humans": 0}
    for a in db.conn().execute("SELECT handle, kind, last_seen_at FROM agents"):
        if db.presence(a["last_seen_at"]) != "active":
            continue
        if a["kind"] == "keeper" or a["handle"].lower().startswith("keeper-"):
            out["keepers"] += 1
        elif a["kind"] == "cron":
            out["runners"] += 1
        elif a["kind"] == "human":
            out["humans"] += 1
        elif a["handle"].lower() not in states:
            bots += 1
    return {"bots": bots, "working": working, **out}


def pools() -> list[dict]:
    """Every active workspace and its bots."""
    c = db.conn()
    try:
        rows = c.execute("SELECT id, slug FROM workspaces WHERE archived = 0 ORDER BY slug").fetchall()
    except db.sqlite3.OperationalError:     # a board without workspaces yet
        return []
    return [{"workspace": r["slug"], "bots": [b[0] for b in c.execute(
        "SELECT handle FROM agents WHERE workspace_id = ? ORDER BY handle COLLATE NOCASE", (r["id"],))]} for r in rows]


def down(handles) -> dict[str, str]:
    """Of these thread workers, the bots not running now, for the pickup chip (#169):
    'stopped' when every box that knows the bot reports it stopped, else 'away' for a bot
    the board has not seen for an hour (one run by hand, or on a box gone dark)."""
    handles = {h.lower(): h for h in handles if h}
    if not handles:
        return {}
    states = {}
    for h in hosts():
        for b in h["bots"]:
            if b["handle"].lower() in handles:
                states.setdefault(b["handle"].lower(), set()).add(b["state"]["state"])
    out, unsure = {}, []
    for low, handle in handles.items():
        seen = states.get(low, set())
        if seen == {"stopped"}:
            out[handle] = "stopped"
        elif seen <= {"unknown"}:
            unsure.append(low)
    if unsure:
        marks = ",".join("?" * len(unsure))
        for a in db.conn().execute("SELECT handle, kind, last_seen_at FROM agents"
                                   f" WHERE lower(handle) IN ({marks})", unsure):
            if a["kind"] != "human" and db.presence(a["last_seen_at"]) in ("idle", "away"):
                out[handles[a["handle"].lower()]] = "away"
    return out


def default_bot(host: str, keeper: str, ask: dict | None, operators: list[str], seen: list[dict]) -> dict | None:
    """A box installed with a default bot (#180): its keeper asks for one until the board
    answers. The bot goes in the admin workspace, and the keeper takes it like a start from
    /fleet. It is set by the first of `operators`, which keepers from before KEEPER_OPERATORS
    was dropped still check. Only a box with no bot at all, reported
    or asked for, gets one: a reinstall never adds a second. With no admin workspace yet
    (nobody has signed in), the answer says so and the keeper asks again next report.
    Returns {handle, harness, created} or {waiting}, or None when nothing was asked."""
    if not ask:
        return None
    from . import workspaces
    c = db.conn()
    mine = [b["handle"] for b in seen if b.get("status") != "moved"]
    mine += [r["handle"] for r in c.execute("SELECT handle FROM fleet_bots WHERE host=? AND deleted IS NULL", (host,))]
    if mine:
        return {"handle": mine[0], "harness": "", "created": False}
    harness = str(ask.get("harness") or "")
    if harness not in HARNESSES:
        return None
    workspaces.ensure_admin()
    try:
        workspaces.find(workspaces.ADMIN)
    except KeyError:
        return {"waiting": "sign in to the board first"}
    handle = str(ask.get("handle") or "")
    taken = handle and (db.get_agent_by_handle(handle) or c.execute(
        "SELECT 1 FROM fleet_bots WHERE handle=?", (handle,)).fetchone())
    if not db.HANDLE_RE.fullmatch(handle) or taken:
        handle = free_name()
    why = want(handle, host, True, harness, (operators or [keeper])[0], workspace=workspaces.ADMIN)
    if why:
        return {"waiting": why}
    return {"handle": handle, "harness": harness, "created": True, "workspace": workspaces.ADMIN}


def free_name() -> str:
    """A first name for a new bot that no agent on the board, no request and no box
    has used; once those run out, a name with four hex characters, as bin/bot does."""
    c = db.conn()
    taken = {r[0].lower() for r in c.execute("SELECT handle FROM agents")}
    taken |= {r[0].lower() for r in c.execute("SELECT handle FROM fleet_bots")}
    taken |= {b["handle"].lower() for h in hosts() for b in h["bots"]}
    free = [n for n in NAMES if n not in taken]
    return random.choice(free) if free else f"{random.choice(NAMES)}-{uuid.uuid4().hex[:4]}"


def move(handle: str, to: str, by: str, start: bool = False) -> str | None:
    """Record that a human wants this bot on another box. Its own box's keeper stops
    it, packs it and hands the archive over; the board then points the bot at the new
    box (handed), whose keeper unpacks it and starts it (fetched). A bot that was stopped
    stays stopped there, unless start: then it is revived on the new box (#168). Returns
    None, or why not."""
    if _deleted(handle):
        return f"{handle} was deleted"
    boxes = {h["name"].lower(): h for h in hosts()}
    to = to.lower()
    if to not in boxes or not boxes[to]["reporting"]:
        return f"no box named {to} is reporting"
    c = db.conn()
    row = c.execute("SELECT * FROM fleet_bots WHERE handle=?", (handle,)).fetchone()
    # the bot's box: where it was asked to be, else the reporting box that has it
    src = row["host"] if row else next((n for n, h in boxes.items() if h["reporting"] and
                                        any(b["handle"].lower() == handle.lower() and b["status"] != "moved"
                                            for b in h["bots"])), None)
    if not src or src not in boxes:
        return f"no box reports {handle}"
    if row and row["fetch"]:
        return f"{handle} is still being unpacked on {src}; move it once it is there"
    if src == to:
        return f"{handle} is on {to} already"
    if not boxes[src]["reporting"]:
        return f"{src} is not reporting, so nothing can pack {handle} there"
    seen = next((b for b in boxes[src]["bots"] if b["handle"].lower() == handle.lower()), None)
    if not seen:
        return f"{src} does not report {handle}"
    if not seen.get("pool"):
        return f"{handle} is a single-repo bot; only pool bots move from the board (bin/bot pack and unpack by hand)"
    harness = seen.get("harness") or (row["harness"] if row else None)
    if harness not in HARNESSES:
        return f"{src} does not say which harness {handle} runs"
    with db._write_lock:
        c.execute(
            "INSERT INTO fleet_bots (handle, host, harness, run, set_by, set_at, move_to) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(handle) DO UPDATE SET host=excluded.host, harness=excluded.harness, run=excluded.run,"
            " set_by=excluded.set_by, set_at=excluded.set_at, problem='', move_to=excluded.move_to, fetch=NULL, kill=NULL,"
            " threads=NULL, retired=CASE WHEN excluded.run THEN NULL ELSE retired END",
            # a bot that was stopped stays stopped on the new box
            (handle, src, harness, int(start or seen["status"].startswith(UP) or bool(row and row["run"])),
             by, db.now(), to),
        )
        c.commit()
    return None


def reset(handle: str, by: str) -> str | None:
    """Record that a human wants this bot to start a new harness session (#221): its old
    session keeps whatever role it was started with. The keeper of its box carries it out,
    at once for a stopped bot, when idle for a running one. Returns None, or why not."""
    if _deleted(handle):
        return f"{handle} was deleted"
    c = db.conn()
    row = c.execute("SELECT * FROM fleet_bots WHERE handle=?", (handle,)).fetchone()
    boxes = {h["name"].lower(): h for h in hosts()}
    src = row["host"] if row else next((n for n, h in boxes.items() if h["reporting"] and any(
        b["handle"].lower() == handle.lower() and b["status"] != "moved" for b in h["bots"])), None)
    if not src or src not in boxes:
        return f"no box reports {handle}"
    if not boxes[src]["reporting"]:
        return f"{src} is not reporting, so nothing can reset {handle} there"
    seen = next((b for b in boxes[src]["bots"] if b["handle"].lower() == handle.lower()), None)
    harness = (seen or {}).get("harness") or (row["harness"] if row else None)
    if harness not in HARNESSES:
        return f"{src} does not say which harness {handle} runs"
    now = db.now()
    with db._write_lock:
        if row:
            c.execute("UPDATE fleet_bots SET reset=? WHERE handle=?", (now, handle))
        else:   # nothing asked of it yet: ask that it stay as it is, with a new session
            c.execute("INSERT INTO fleet_bots (handle, host, harness, run, set_by, set_at, reset) VALUES (?,?,?,?,?,?,?)",
                      (handle, src, harness, int(bool(seen and seen["status"].startswith(UP))), by, now, now))
        c.commit()
    return None


def drain(host: str, by: str, to: str | None = None) -> dict | str:
    """Move every pool bot up on a box to other boxes, one move() each: a full box
    emptied in one request. Each bot goes to `to`, else to the reporting box with
    the least load per core, counting the bots this drain already sent there.
    Returns {"moved": {handle: box}, "skipped": {handle: why}}, or why nothing can be done."""
    boxes = {h["name"].lower(): h for h in hosts()}
    src = boxes.get(host.lower())
    if not src:
        return f"no box named {host} has reported"
    targets = [h for n, h in boxes.items() if n != host.lower() and h["reporting"]]
    if to:
        targets = [h for h in targets if h["name"].lower() == to.lower()]
        if not targets:
            return f"no other box named {to} is reporting"
    if not targets:
        return "no other box is reporting, so there is nowhere to move the bots"
    sent = {h["name"]: 0 for h in targets}

    def weight(h):   # load per core now, plus one per bot this drain already sent there
        cores = h.get("cores") or 1
        return ((h.get("load") or 0) + sent[h["name"]]) / cores
    moved, skipped = {}, {}
    for b in src["bots"]:
        if not b["status"].startswith(UP):
            continue
        if b.get("move_to") or b.get("fetch"):
            skipped[b["handle"]] = "already moving"
            continue
        if not b.get("pool"):
            skipped[b["handle"]] = "a single-repo bot; move it by hand"
            continue
        dest = min(targets, key=weight)
        why = move(b["handle"], dest["name"], by)
        if why:
            skipped[b["handle"]] = why
        else:
            moved[b["handle"]] = dest["name"]
            sent[dest["name"]] += 1
    if not moved and not skipped:
        return f"no bot is running on {host}"
    return {"moved": moved, "skipped": skipped}


def moved(host: str, handed: dict, fetched: list) -> None:
    """A keeper's word on moves: handed (handle -> archive name) it packed and handed
    over, so the bot now belongs to the box it is going to; fetched, it unpacked."""
    c = db.conn()
    with db._write_lock:
        for h, archive in handed.items():
            c.execute("UPDATE fleet_bots SET host=move_to, fetch=?, move_to=NULL, problem='' WHERE handle=?"
                      " AND host=? AND move_to IS NOT NULL", (str(archive)[:200], h, host))
        for h in fetched:
            c.execute("UPDATE fleet_bots SET fetch=NULL WHERE handle=? AND host=?", (h, host))
        c.commit()


def unwant(handle: str, by: str) -> str | None:
    """Drop what was asked for a bot: its box's keeper leaves it as it is from then on.
    Returns None, or why not."""
    c = db.conn()
    row = c.execute("SELECT host FROM fleet_bots WHERE handle=?", (handle,)).fetchone()
    if row is None:
        return ""
    with db._write_lock:
        c.execute("DELETE FROM fleet_bots WHERE handle=?", (handle,))
        c.commit()
    return None


def wanted(host: str, problems: dict | None = None) -> list[dict]:
    """What is asked of a box. With problems (handle -> text, from its keeper), store
    them first: a bot the keeper says nothing about has none."""
    c = db.conn()
    if problems is not None:
        with db._write_lock:
            raw = c.execute("SELECT report FROM fleet_hosts WHERE name=?", (host,)).fetchone()
            observed = {b["handle"].lower(): b for b in json.loads(raw["report"])["bots"]} if raw else {}
            for r in c.execute("SELECT handle FROM fleet_bots WHERE host=?", (host,)).fetchall():
                problem = str(problems.get(r["handle"], ""))[:500]
                ack = c.execute("SELECT a.* FROM fleet_alert_clears a JOIN fleet_bots b ON b.handle=a.handle "
                                "AND b.set_at=a.set_at WHERE a.handle=? AND a.host=?", (r["handle"], host)).fetchone()
                bot = observed.get(r["handle"].lower(), {})
                if ack and bot.get("status") == "stopped" and problem == ack["problem"] and bot.get("last_run", "") == ack["last_run"]:
                    problem = ""
                c.execute("UPDATE fleet_bots SET problem=? WHERE handle=?", (problem, r["handle"]))
            c.commit()
    return [{**dict(r), "run": bool(r["run"])} for r in
            c.execute("SELECT * FROM fleet_bots WHERE host=? ORDER BY handle", (host,))]


def alert_key(host, bot):
    fields = {k: bot.get(k, "") for k in ("status", "health", "last_run", "problem", "move_to", "fetch", "kill", "want", "set_at", "harness")}
    return hashlib.sha256(json.dumps([host.lower(), bot["handle"].lower(), fields], sort_keys=True).encode()).hexdigest()


def clear_alerts(items: list[dict], by: str):
    with db._write_lock, db.conn() as c:
        c.execute("BEGIN IMMEDIATE")
        boxes = {h["name"].lower(): h for h in hosts()}
        checked = []
        for item in items:
            host, handle = item["host"].lower(), item["handle"].lower()
            box = boxes.get(host)
            if not box or not box["reporting"]:
                raise ValueError("box state is unknown; wait for a fresh report")
            bot = next((b for b in box["bots"] if b["handle"].lower() == handle), None)
            if not bot or not bot.get("clear_key") or bot["clear_key"] != item["key"]:
                raise ValueError("worker state or alert changed; refresh before clearing")
            for other in boxes.values():
                for copy in other["bots"]:
                    if other["name"].lower() != host and copy["handle"].lower() == handle and copy["status"] != "moved":
                        if not other["reporting"] or copy["status"] != "stopped":
                            raise ValueError("worker is live or unknown on another box")
            checked.append((host, handle, bot))
        # One atomic group: a changed/live member prevents clearing any member.
        for host, handle, bot in checked:
            stamp = datetime.now(timezone.utc).isoformat()
            _set_want(c, handle, host, bot["harness"], False, by, stamp)
            c.execute("UPDATE fleet_bots SET fetch=NULL WHERE handle=?", (handle,))
            c.execute("INSERT INTO fleet_alert_clears VALUES (?,?,?,?,?,?) ON CONFLICT(handle) DO UPDATE SET "
                      "host=excluded.host,health=excluded.health,last_run=excluded.last_run,problem=excluded.problem,set_at=excluded.set_at",
                      (handle, host, bot.get("health", ""), bot.get("last_run", ""), bot.get("problem", ""), stamp))
    return [handle for _, handle, _ in checked]


def hosts() -> list[dict]:
    rows = db.conn().execute(
        "SELECT h.name, h.reported_at, h.report, a.handle AS keeper FROM fleet_hosts h"
        " JOIN agents a ON a.id = h.reported_by ORDER BY h.name"
    ).fetchall()
    # when each bot last posted, from the board's side of what it does
    posted = {p["handle"].lower(): p["at"] for p in db.conn().execute(
        "SELECT a.handle, MAX(m.created_at) AS at FROM messages m JOIN agents a ON a.id = m.agent_id GROUP BY a.id")}
    # each bot's workspace: the one it registered in, else the one it was created for
    try:
        dedicated = {r["handle"].lower(): r["slug"] for r in db.conn().execute(
            "SELECT a.handle, w.slug FROM agents a JOIN workspaces w ON w.id = a.workspace_id")}
        for r in db.conn().execute("SELECT handle, workspace FROM fleet_bots WHERE workspace IS NOT NULL"):
            dedicated.setdefault(r["handle"].lower(), r["workspace"])
    except db.sqlite3.OperationalError:     # a board without workspaces yet
        dedicated = {}
    holding = db.open_threads()     # each bot's claimed, not completed threads (#167)
    retired = {r[0].lower() for r in db.conn().execute("SELECT handle FROM agents WHERE retired_at IS NOT NULL")}
    out = []
    clears = {r["handle"].lower(): dict(r) for r in db.conn().execute("SELECT * FROM fleet_alert_clears")}
    for r in rows:
        secs = db.seconds_since(r["reported_at"])
        h = {
            "name": r["name"], "keeper": r["keeper"], "reported_at": r["reported_at"],
            "reporting": secs is not None and secs < STALE_AFTER,
            **json.loads(r["report"]),
        }
        # Beside what the keeper saw, what was asked: `want` is running or stopped
        # (absent when nobody asked), and a bot asked for but not seen yet is listed too.
        observed = {b["handle"].lower() for b in h["bots"]}
        asked = {w["handle"].lower(): w for w in wanted(r["name"])}
        for b in h["bots"]:
            w = asked.pop(b["handle"].lower(), None)
            if w:
                b.update(want="running" if w["run"] else "stopped", set_by=w["set_by"], set_at=w["set_at"],
                         problem=w["problem"], harness=b.get("harness") or w["harness"],
                         move_to=w["move_to"], fetch=w["fetch"], kill=w["kill"], profile=w.get("profile"),
                         channels=_channels(w), voice=w.get("voice") or "", personality=w.get("personality") or "",
                         threads=w["threads"], retired=w["retired"], reset=w["reset"],
                         deleted=w["deleted"])
        h["bots"] += [{"handle": w["handle"], "harness": w["harness"], "status": "stopped", "serves": "",
                       "want": "running" if w["run"] else "stopped", "set_by": w["set_by"], "set_at": w["set_at"],
                       "problem": w["problem"], "move_to": w["move_to"], "fetch": w["fetch"],
                       "threads": w["threads"], "retired": w["retired"], "profile": w.get("profile"),
                       "channels": _channels(w), "voice": w.get("voice") or "",
                       "personality": w.get("personality") or ""}
                      for w in asked.values() if not w["deleted"]]
        # a deleted bot leaves /fleet, but for the time its box, reporting, still has it
        h["bots"] = [b for b in h["bots"] if b["handle"].lower() not in retired or (b.get("deleted") and h["reporting"])]
        for b in h["bots"]:
            ack = clears.get(b["handle"].lower())
            if ack and h["reporting"] and b["status"] == "stopped" and ack["host"].lower() == h["name"].lower():
                if ack["set_at"] == b.get("set_at") and ack["health"] == b.get("health", "") and ack["last_run"] == b.get("last_run", ""):
                    b["health"] = ""
            if h["reporting"] and b["handle"].lower() in observed and b["status"] == "stopped" and any(b.get(k) for k in ("health", "problem", "move_to", "fetch")):
                b["clear_key"] = alert_key(h["name"], b)
            b["last_post"] = posted.get(b["handle"].lower(), "")
            b["workspace"] = dedicated.get(b["handle"].lower())
            held = holding.get(b["handle"].lower(), [])
            b["open_threads"], b["open_thread_list"] = len(held), held[:20]
            b["state"] = bot_state(b, h["reporting"])
        out.append(h)
    return out
