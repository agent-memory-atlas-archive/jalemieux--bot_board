"""Routines: recurring work, stored on the board and run by bin/routines.

A routine is a row here, not a message: only the API changes it, and nobody has
to read machinery in a channel. What people do read stays in channels: the
conversation in which a human asks the admin bot for a routine (its `thread`),
the jobs the runner fires into workspace channels, and a notice in the admin
channel when one fails.

Who may change one: a human who manages the admin workspace (or the board), or
an admin bot, a bot of that workspace. The trust boundary is the check: it is
code that runs on the runner's box with the fleet's secrets, so a check a bot
wrote runs only once a human approved it. The board keeps the last approved
definition beside the current one (`approved_definition`). A version whose check
differs from it waits (`waiting`): the board lists it on /routines with the diff
and an Approve button, counts it in the bell, and tells the admin workspace's
owner in the routine's thread (#380). Approve stores the yes, asks the runner for
a test, and the routine is enabled again when the test passes, if it was enabled
before the change. A change that leaves the check alone (the prompt, the timing,
the name) is a new version that keeps running: no yes needed.

The older way still works for an admin bot: its enable and test may cite
`approved_by`, the id of a human manager's reply in the routine's thread posted
after the current version was written (a new version may move the thread to
where it is discussed). Once a version is approved either way, test and enable
need nothing more. The runner (the ROUTINES_HANDLE agent) reads the routines and
reports tests and failures back; whether one runs is its state here.

A release whose recipe writer is newer (RECIPE_VERSION) drafts an upgrade of each
recipe routine an older one wrote: the same settings, the check and prompt this
release writes. It is not stored, only shown with its diff (`upgrade`), and the
routine keeps running what was approved until a human manager approves the
upgrades they were shown, in the same list, one or all at once (#242). A bot
cannot: that yes is the trust boundary for code that runs with the fleet's secrets.

The definition's validation is the runner's own code (bin/routines, loaded as a
module), so both ends agree on what can run.
"""
from __future__ import annotations

import difflib
import importlib.machinery
import importlib.util
import json
import os
import threading
import time

from . import db, fleet, workspaces

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "routines")


def _load():
    loader = importlib.machinery.SourceFileLoader("bot_board_routines", _PATH)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


cli = _load()
DEFAULTS = cli.DEFAULTS
ADMIN_PROJECT = workspaces.ADMIN      # the admin workspace's slug
JOBS_SHOWN = 5
ITEMS_SHOWN = 30
FIELDS = ("name", "channel", "check", "prompt", "every", "claim_timeout", "done_timeout", "resends",
          "about", "skill", "recipe")

ROUTINES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "routines")

SCHEMA = """
CREATE TABLE IF NOT EXISTS routines_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY,
    definition TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'paused',
    version INTEGER NOT NULL DEFAULT 1,
    defined_at INTEGER NOT NULL,
    thread_id INTEGER REFERENCES messages(id),
    approved_by INTEGER REFERENCES messages(id),
    approved_definition TEXT,
    resume INTEGER NOT NULL DEFAULT 0,
    approving INTEGER NOT NULL DEFAULT 0,
    failure TEXT,
    test_asked INTEGER NOT NULL DEFAULT 0,
    last_test TEXT,
    run_asked INTEGER NOT NULL DEFAULT 0,
    last_run TEXT,
    created_by INTEGER NOT NULL REFERENCES agents(id),
    updated_by INTEGER NOT NULL REFERENCES agents(id),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def start_runner(interval: int = 30) -> threading.Thread:
    """Run the runner inside the board, as a daemon thread (BOT_BOARD_ROUTINES=1).

    It is the same code as `bin/routines run`, talking to this board over HTTP as
    the `routines` agent. The board gives it a token itself, written where the
    runner looks (ROUTINES_CONFIG_DIR), so it survives restarts and releases. A
    new token for `routines` also locks out a runner still running elsewhere
    under that handle: there is only ever one. Checks get the fleet's secrets (GH_TOKEN,
    ...) as every box's bots do, read from the board at each run, so they are set once,
    on /fleet; they win over the container's own environment."""
    cli.secrets = fleet.secret_values
    path = os.path.join(cli.CFG, f"{cli.ME}.token")
    if not (os.path.isfile(path) and os.path.getsize(path)):
        agent = db.get_agent_by_handle(cli.ME)
        token = db.rotate_token(agent["id"]) if agent else db.register_agent(
            cli.ME, "cron", "runs the routines inside the board: checks on a timer, "
            "posts work when there is some")[1]
        os.makedirs(cli.CFG, mode=0o700, exist_ok=True)
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            fh.write(token + "\n")

    def loop():
        runner = None
        while True:
            try:
                runner = runner or cli.Runner()   # the board may not be answering yet at startup
                runner.tick()
            except Exception as e:                # never let the thread die
                print(f"routines: pass failed, will retry: {type(e).__name__}: {e}", flush=True)
            time.sleep(interval)

    t = threading.Thread(target=loop, name="routines-runner", daemon=True)
    t.start()
    return t


def init():
    with db._write_lock:
        c = db.conn()
        c.executescript(SCHEMA)
        have = {r[1] for r in c.execute("PRAGMA table_info(routines)")}
        for col, ddl in (("run_asked", "INTEGER NOT NULL DEFAULT 0"), ("last_run", "TEXT"),      # #209
                         ("approved_definition", "TEXT"), ("resume", "INTEGER NOT NULL DEFAULT 0"),  # #380
                         ("approving", "INTEGER NOT NULL DEFAULT 0")):
            if col not in have:
                c.execute(f"ALTER TABLE routines ADD COLUMN {col} {ddl}")
        if "approved_definition" not in have:
            # a board from before #380: what runs, or was said yes to, counts as approved
            c.execute("UPDATE routines SET approved_definition=definition"
                      " WHERE state IN ('enabled','failed') OR approved_by IS NOT NULL")
        c.commit()


# ---------------------------------------------------------------- the admin workspace

ensure_admin_project = workspaces.ensure_admin      # the admin workspace comes with the board


# ---------------------------------------------------------------- shipped routines

def shipped() -> list[tuple[str, dict]]:
    """The routines the board ships, under docs/routines/<name>/: routine.json (the settings),
    `check` (the script) and prompt.md. [(name, definition)], in name order."""
    out = []
    if not os.path.isdir(ROUTINES_DIR):
        return out
    for name in sorted(os.listdir(ROUTINES_DIR)):
        d = os.path.join(ROUTINES_DIR, name)
        try:
            with open(os.path.join(d, "routine.json"), encoding="utf-8") as fh:
                settings = json.load(fh)
            with open(os.path.join(d, "check"), encoding="utf-8") as fh:
                check = fh.read()
            with open(os.path.join(d, "prompt.md"), encoding="utf-8") as fh:
                prompt = fh.read()
        except (OSError, ValueError):
            continue
        if not isinstance(settings, dict):
            continue
        out.append((name, {**settings, "name": settings.get("name") or name, "check": check, "prompt": prompt}))
    return out


def seed() -> list[str]:
    """Store each shipped routine once, paused, in the admin workspace's channel: the board's
    own chores (daily care) come with the board, and a human says yes before one runs. Once
    per name, so deleting a seeded routine sticks; a board from before a routine was shipped
    gets it at its next start. Nothing happens until the admin workspace exists; its owner,
    the board's first human, is the author. Returns the names stored now."""
    try:
        ws = workspaces.find(ADMIN_PROJECT)
    except KeyError:
        return []
    author = db.get_agent_by_id(ws["owner_id"])     # the admin workspace's owner: the board's first human
    if not author:
        return []
    stored = []
    c = db.conn()
    for name, definition in shipped():
        key = f"seeded:{name}"
        if c.execute("SELECT 1 FROM routines_meta WHERE key=?", (key,)).fetchone():
            continue
        try:
            r = _definition({**definition, "channel": definition.get("channel") or workspaces.home_slug(ws)})
        except (ValueError, KeyError) as e:
            print(f"bot_board: shipped routine {name} not stored: {e}", flush=True)
            continue
        with db._write_lock, db.conn() as w:
            w.execute("INSERT INTO routines(definition,defined_at,resume,created_by,updated_by,created_at,updated_at)"
                      " VALUES (?,?,1,?,?,?,?)", (json.dumps(r), db.cursor(), author["id"], author["id"], db.now(), db.now()))
            w.execute("INSERT OR REPLACE INTO routines_meta(key,value) VALUES (?,?)", (key, db.now()))
        stored.append(name)
    return stored


def admins() -> list[str]:
    """The admin bots: the bots of the admin workspace."""
    try:
        return workspaces.bots_of(workspaces.find(ADMIN_PROJECT))
    except KeyError:
        return []


is_operator = workspaces.is_operator
is_admin_bot = workspaces.is_admin_bot


def is_runner(agent: dict) -> bool:
    return agent["handle"] == cli.ME


def _require_switch(agent: dict) -> None:
    if not (is_operator(agent) or is_admin_bot(agent)):
        raise PermissionError(f"only a human who manages the `{ADMIN_PROJECT}` workspace, or an admin bot (a bot "
                              "of that workspace), changes routines")


# ---------------------------------------------------------------- reading

def _row(rid: int) -> dict:
    row = db.conn().execute("SELECT * FROM routines WHERE id=? AND state<>'deleted'", (rid,)).fetchone()
    if row is None:
        raise KeyError(f"no routine {rid}")
    return dict(row)


def _jobs(r: dict) -> list[dict]:
    """The latest messages the runner fired for this routine, and how each went."""
    rows = db.conn().execute(
        db._MSG_SELECT + " WHERE b.slug=? AND m.reply_to IS NULL AND json_extract(m.meta,'$.routine')=?"
        " AND json_extract(m.meta,'$.routine_item') IS NULL ORDER BY m.id DESC LIMIT ?",
        (r["channel"], r["id"], JOBS_SHOWN)).fetchall()
    out = []
    for m in db._hydrate(rows):
        replies = db.list_messages(thread=m["id"], since=m["id"], limit=500)
        done = next((x for x in replies if x["meta"].get("done") is True
                     and x["author"] != cli.ME and x["author_kind"] != "human"), None)
        out.append({"id": m["id"], "channel": m["channel"], "created_at": m["created_at"],
                    "sends": 1 + sum(1 for x in replies if x["author"] == cli.ME and "resend" in x["meta"]),
                    "done_by": done and done["author"], "done_at": done and done["created_at"]})
    return out


def _items(r: dict) -> list[dict]:
    """The issues a recipe routine announced, newest first, with how many replies each has and
    who holds the thread: its worker, since when, and that worker's last post there (#234)."""
    rows = db.conn().execute(
        "SELECT m.id, b.slug AS channel, m.created_at, json_extract(m.meta,'$.routine_item.number') AS number,"
        " (SELECT COUNT(*) FROM messages x WHERE x.thread_id=m.id AND x.id<>m.id) AS replies,"
        " a.handle AS worker, w.claimed_at AS worker_claimed_at,"
        " (SELECT MAX(x.created_at) FROM messages x WHERE x.thread_id=m.id AND x.id<>m.id"
        "  AND x.agent_id=w.agent_id) AS worker_last_post"
        " FROM messages m JOIN boards b ON b.id=m.board_id"
        " LEFT JOIN thread_workers w ON w.thread_id=m.id LEFT JOIN agents a ON a.id=w.agent_id"
        " WHERE m.reply_to IS NULL"
        " AND json_extract(m.meta,'$.routine')=? AND json_extract(m.meta,'$.routine_item') IS NOT NULL"
        " ORDER BY m.id DESC LIMIT ?", (r["id"], ITEMS_SHOWN)).fetchall()
    return [dict(x) for x in rows]


def _stored(row: dict) -> dict:
    """The definition as this release reads it, whichever release stored it."""
    return cli.upgraded(json.loads(row["definition"]))


def _approved(row: dict) -> dict | None:
    """The definition a human last approved, as this release reads it; None when none was."""
    return cli.upgraded(json.loads(row["approved_definition"])) if row.get("approved_definition") else None


def _waiting(row: dict) -> bool:
    """Does this version need a human's yes: its check is not the approved one (#380)."""
    ok = _approved(row)
    return ok is None or ok["check"] != _stored(row)["check"]


def _changed(old: dict | None, new: dict) -> str:
    """One line on what a version changes against the approved one: `check (3 lines), prompt, every`."""
    if old is None:
        return "new"
    out = []
    for k in ("check", "prompt"):
        if old.get(k) != new.get(k):
            n = sum(1 for line in difflib.unified_diff((old.get(k) or "").splitlines(), (new.get(k) or "").splitlines(),
                                                       lineterm="", n=0) if line[:1] in "+-" and line[:3] not in ("+++", "---"))
            out.append(f"{k} ({n} line{'s' if n != 1 else ''})")
    out += [k for k in FIELDS + ("recipe_version",) if k not in ("check", "prompt") and old.get(k) != new.get(k)]
    return ", ".join(out) or "nothing"


def describe(row: dict, full: bool = True) -> dict:
    d = _stored(row)
    test = json.loads(row["last_test"]) if row["last_test"] else None
    ran = json.loads(row["last_run"]) if row["last_run"] else None
    out = {**d, "id": row["id"], "state": row["state"], "version": row["version"], "thread": row["thread_id"],
           "approved_by": row["approved_by"], "failure": row["failure"] if row["state"] == "failed" else None,
           "test_pending": row["test_asked"] > (test or {}).get("for", 0), "test_asked": row["test_asked"],
           "last_test": test, "run_pending": row["run_asked"] > (ran or {}).get("for", 0),
           "run_asked": row["run_asked"], "last_run": ran, "problem": cli.problem_with(d),
           "created_at": row["created_at"], "updated_at": row["updated_at"],
           "approving": row["approving"] > (test or {}).get("for", 0)}
    if _waiting(row):
        # a version whose check no human has approved: shown on /routines with the diff and Approve (#380)
        ok = _approved(row)
        out["waiting"] = {"changed": _changed(ok, d), "diff": _diff(ok, d) if ok else "", "resume": bool(row["resume"]),
                          "by": (db.get_agent_by_id(row["updated_by"]) or {}).get("handle")}
    up = _upgrade(d)
    if up and row["state"] != "deleted":
        out["upgrade"] = {"from": d["recipe_version"], "to": up["recipe_version"], "diff": _diff(d, up)}
    if full:
        out.update(jobs=_jobs(out), items=_items(out) if isinstance(d.get("recipe"), dict) else [])
    return out


def _upgrade(d: dict) -> dict | None:
    """The definition this release's recipe writer gives a recipe routine an older one wrote:
    its settings as stored, its check and prompt written again. None when there is none to draft."""
    rc, rv = d.get("recipe"), d.get("recipe_version")
    if not isinstance(rc, dict) or type(rv) is not int or rv >= cli.RECIPE_VERSION or cli.recipe_problem(rc):
        return None
    up = {**d, "recipe_version": cli.RECIPE_VERSION, "check": cli.recipe_check(rc), "prompt": cli.recipe_prompt(rc)}
    return None if cli.problem_with(up) else up


def _diff(old: dict, new: dict, was: str = "approved", now: str = "this version") -> str:
    """What a version changes, as a unified diff of the prompt and then the check."""
    if "recipe_version" in old and "recipe_version" in new and old["recipe_version"] != new["recipe_version"]:
        was, now = f"recipe v{old['recipe_version']}", f"recipe v{new['recipe_version']}"
    return "".join(line for k in ("prompt", "check") for line in difflib.unified_diff(
        (old[k] + "\n").splitlines(True), (new[k] + "\n").splitlines(True), f"{k} ({was})", f"{k} ({now})"))


def list_routines(full: bool = True) -> list[dict]:
    rows = db.conn().execute("SELECT * FROM routines WHERE state<>'deleted' ORDER BY id").fetchall()
    return [describe(dict(r), full) for r in rows]


def get(rid: int) -> dict:
    return describe(_row(rid))


# ---------------------------------------------------------------- writing

def _definition(data: dict, old: dict | None = None) -> dict:
    """A complete definition from what was sent (over `old`, for a change), or ValueError."""
    text = lambda v: v.replace("\r\n", "\n") if isinstance(v, str) else v
    sent = {k: text(data[k]) for k in FIELDS if data.get(k) is not None}
    r = {**DEFAULTS, **(old or {}), **sent}
    for k in ("name", "channel", "prompt", "about", "skill"):
        if isinstance(r.get(k), str):
            r[k] = r[k].strip()
    r["channel"] = (r.get("channel") or "").lower().lstrip("#")
    for k in ("about", "skill"):
        if not r.get(k):
            r.pop(k, None)
    if "recipe" in sent or (old and "recipe" in old and "check" not in sent and "prompt" not in sent):
        # The board writes the check and the prompt from the recipe; the runner checks they still match.
        if not isinstance(r["recipe"], dict):
            raise ValueError("`recipe` must be an object")
        rc = cli.recipe_of(r["recipe"])
        problem = cli.recipe_problem(rc)
        if problem:
            raise ValueError(problem)
        ws = workspaces.find(rc["workspace"])   # KeyError when there is none
        if ws["archived"]:
            raise ValueError(f"the workspace `{rc['workspace']}` is archived")
        r.update(recipe=rc, recipe_version=cli.RECIPE_VERSION, check=cli.recipe_check(rc), prompt=cli.recipe_prompt(rc))
        r["channel"] = r["channel"] or workspaces.home_slug(ws)
    else:
        r.pop("recipe", None)
        r.pop("recipe_version", None)
    problem = cli.problem_with(r)
    if problem:
        raise ValueError(problem)
    return r


def _thread(mid: int | None) -> int | None:
    """The thread a message is in: where the human's yes is looked for."""
    if mid is None:
        return None
    m = db.get_message(mid)
    if m is None:
        raise KeyError(f"no message {mid}")
    return m["thread_id"] or m["id"]


def _owner() -> dict | None:
    """The human the board asks for a yes: the admin workspace's owner."""
    try:
        return db.get_agent_by_id(workspaces.find(ADMIN_PROJECT)["owner_id"])
    except KeyError:
        return None


def _say(row: dict, body: str, meta: dict) -> dict | None:
    """The board's own line in the routine's thread, if it has one: the ask for a yes and
    what came of it. Posted as the board, so it does not rest on a bot being awake (#380)."""
    from . import progress
    root = row["thread_id"] and db.get_message(row["thread_id"])
    if not root:
        return None
    try:
        return db.post_message(root["channel"], progress.board_agent()["id"], body, reply_to=root["id"],
                               meta={**meta, "routine": row["id"], "version": row["version"]})
    except (PermissionError, KeyError, ValueError) as e:      # an archived or closed thread: the bell still asks
        print(f"routines: could not post in thread {root['id']}: {e}", flush=True)
        return None


def _ask(rid: int, agent: dict) -> None:
    """A version a bot stored waits for a human: tell the owner where to approve it, in the thread
    and in their inbox. A human's own version needs no note: they are on the page already."""
    row = _row(rid)
    if is_operator(agent) or not _waiting(row):
        return
    r = describe(row, full=False)
    who = _owner()
    at = "@" + who["handle"] + " " if who else ""
    _say(row, f"{at}version {row['version']} of routine `{r['name']}` ({r['waiting']['changed']}) waits for your yes. "
              f"Approve it on /routines#approvals: the runner tests it" +
              (" and it is enabled again" if row["resume"] else ", then it can be enabled") + ".",
         {"needs": who["handle"], "ask": f"approve version {row['version']} of routine {r['name']} on /routines#approvals"}
         if who else {})


def create(data: dict, agent: dict) -> dict:
    """A new routine, paused, waiting for a human's yes; it comes on once approved and tested.
    `thread`: the message where it was asked for."""
    _require_switch(agent)
    r = _definition(data)
    thread = _thread(data.get("thread"))
    with db._write_lock, db.conn() as c:
        # resume: asked for to run, so it comes on by itself once approved and its test passed
        rid = c.execute("INSERT INTO routines(definition,defined_at,thread_id,resume,created_by,updated_by,created_at,updated_at)"
                        " VALUES (?,?,?,1,?,?,?,?)", (json.dumps(r), db.cursor(), thread, agent["id"], agent["id"],
                                                    db.now(), db.now())).lastrowid
    _ask(rid, agent)
    return get(rid)


def update(rid: int, data: dict, agent: dict) -> dict:
    """A new version of the definition. One that changes the check is paused until a human approves
    it, and comes back on by itself when they do, if it was on; one that leaves the check alone
    keeps the state and the yes it has (#380).

    `thread`: the message where this version is discussed, whose thread then holds the yes (#205).
    """
    _require_switch(agent)
    row = _row(rid)
    old = _stored(row)
    r = _definition(data, old)
    thread = _thread(data.get("thread")) or row["thread_id"]
    same = r["check"] == old["check"] and not _waiting(row)
    with db._write_lock, db.conn() as c:
        # a test asked for the old version is dropped with it: the runner never tests a definition
        # nobody has seen (test_pending is test_asked past the last test's `for`)
        if same:
            c.execute("UPDATE routines SET definition=?, approved_definition=?, version=version+1, defined_at=?, thread_id=?,"
                      " test_asked=COALESCE(json_extract(last_test,'$.for'),0), approving=0, updated_by=?, updated_at=?"
                      " WHERE id=?", (json.dumps(r), json.dumps(r), db.cursor(), thread, agent["id"], db.now(), rid))
        else:
            c.execute("UPDATE routines SET definition=?, version=version+1, defined_at=?, thread_id=?, state='paused',"
                      " resume=CASE WHEN state='enabled' THEN 1 ELSE resume END,"
                      " approved_by=NULL, failure=NULL, test_asked=COALESCE(json_extract(last_test,'$.for'),0), approving=0,"
                      " updated_by=?, updated_at=? WHERE id=?",
                      (json.dumps(r), db.cursor(), thread, agent["id"], db.now(), rid))
    _ask(rid, agent)
    return get(rid)


def _approval(row: dict, agent: dict, approved_by: int | None, action: str) -> int | None:
    """The human's yes an admin bot's enable or test rests on; a human operator needs none, and
    neither does anyone once the version's check is the approved one."""
    if is_operator(agent) or not _waiting(row):
        return approved_by
    yes = db.get_message(approved_by) if approved_by else None
    author = yes and db.get_agent_by_handle(yes["author"])
    if yes is None or row["thread_id"] is None or (yes["thread_id"] or yes["id"]) != row["thread_id"] \
            or not is_operator(author) or yes["id"] <= row["defined_at"]:
        where = (f"in thread {row['thread_id']}" if row["thread_id"] else
                 "in this routine's thread, and it has none: PATCH it with the `thread` where it is discussed")
        raise ValueError(f"an admin bot's {action} needs `approved_by`: the id of a reply from a human who manages "
                         f"the `{ADMIN_PROJECT}` workspace, {where}, posted after its current version")
    return approved_by


def act(rid: int, action: str, agent: dict, approved_by: int | None = None) -> dict:
    """approve, enable, pause, test, run or delete. approve: a human manager's yes to a waiting
    version, from /routines (#380): the runner tests it, and `report` enables it when the test
    passes if it was enabled before the change. enable approves too, and runs it now. run: the
    runner checks an enabled routine on its next pass as if `every` had passed, and fires a real
    job if there is work. It needs no approval: it runs the version already enabled (#209)."""
    _require_switch(agent)
    row = _row(rid)
    sets, args = [], []
    if action == "approve":
        if not is_operator(agent):
            raise PermissionError(f"only a human who manages the `{ADMIN_PROJECT}` workspace approves a routine")
        if not _waiting(row):
            raise ValueError(f"version {row['version']} of routine {rid} is approved already")
        problem = cli.problem_with(_stored(row))
        if problem:
            raise ValueError(f"this definition cannot run: {problem}")
        sets = ["approved_definition=definition", "test_asked=test_asked+1", "approving=test_asked+1"]
    elif action == "enable":
        problem = cli.problem_with(_stored(row))
        if problem:
            raise ValueError(f"this definition cannot run: {problem}")
        sets = ["state='enabled'", "failure=NULL", "approved_by=COALESCE(?, approved_by)", "approved_definition=definition",
                "resume=0", "approving=0"]
        args = [_approval(row, agent, approved_by, action)]
    elif action == "test":
        _approval(row, agent, approved_by, action)
        sets = ["test_asked=test_asked+1"]
    elif action == "run":
        if row["state"] != "enabled":
            raise ValueError("only an enabled routine runs now: enable it first")
        sets = ["run_asked=run_asked+1"]
    elif action == "pause":
        sets = ["state='paused'", "resume=0"]
    elif action == "delete":
        sets = ["state='deleted'"]
    else:
        raise ValueError(f"unknown action {action}")
    with db._write_lock, db.conn() as c:
        # the version read above is the one acted on: a change that landed in between is not enabled unseen
        changed = c.execute(f"UPDATE routines SET {', '.join(sets)}, updated_by=?, updated_at=? WHERE id=? AND version=?",
                            (*args, agent["id"], db.now(), rid, row["version"])).rowcount
    if not changed:
        raise ValueError(f"routine {rid} changed while you were acting on version {row['version']}: read it again")
    if action in ("approve", "enable") and _waiting(row):
        # the record of the yes, in the thread where the bot asked; the inbox ask it answers is closed with it
        said = _say(row, f"Version {row['version']} approved by {agent['handle']}. " +
                    ("The runner tests it now" + (", then it is enabled again." if row["resume"] else
                     "; it stays paused until someone enables it.") if action == "approve" else "Enabled."),
                    {"resolved": True})
        if said:
            with db._write_lock, db.conn() as c:
                c.execute("UPDATE routines SET approved_by=COALESCE(approved_by, ?) WHERE id=? AND version=?",
                          (said["id"], rid, row["version"]))
    return {"deleted": True, "id": rid} if action == "delete" else get(rid)


def approve_upgrades(data: dict, agent: dict) -> dict:
    """A human manager's one yes for the drafted upgrades they were shown (#242):
    {"recipe_version": the version the page showed, "routines": {"<id>": <version shown>}}.
    Each becomes a new version written by this release, in the state it had; one changed
    since it was shown, or with nothing to upgrade, is left as it is and said."""
    _require_switch(agent)
    if not is_operator(agent):
        raise PermissionError(f"only a human who manages the `{ADMIN_PROJECT}` workspace approves routine upgrades")
    if data.get("recipe_version") != cli.RECIPE_VERSION:
        raise ValueError(f"the board now writes recipe version {cli.RECIPE_VERSION}: load the upgrades again and look")
    shown = data.get("routines")
    if not isinstance(shown, dict) or not shown:
        raise ValueError("`routines` must map each routine id to the version you were shown")
    done, left = [], []
    with db._write_lock, db.conn() as c:
        for rid, version in shown.items():
            row = c.execute("SELECT * FROM routines WHERE id=? AND state<>'deleted'",
                            (int(rid) if str(rid).isdigit() else -1,)).fetchone()
            up = row and row["version"] == version and _upgrade(_stored(dict(row)))
            if not up:
                left.append({"id": rid, "why": "no such routine" if not row else "changed since you looked"
                             if row["version"] != version else "nothing to upgrade"})
                continue
            c.execute("UPDATE routines SET definition=?, approved_definition=?, version=version+1, defined_at=?,"
                      " updated_by=?, updated_at=? WHERE id=?",
                      (json.dumps(up), json.dumps(up), db.cursor(), agent["id"], db.now(), row["id"]))
            done.append(row["id"])
    return {"upgraded": done, "left": left}


# ---------------------------------------------------------------- the runner reports

def report(rid: int, data: dict, agent: dict) -> dict:
    """From the runner: a test's result {test: {...}}, a run now's {run: {for, result}}, or
    {failed: why, version} to stop it."""
    if not is_runner(agent):
        raise PermissionError(f"only the runner (@{cli.ME}) reports on routines")
    row = _row(rid)
    test = data.get("test") if isinstance(data.get("test"), dict) else None
    # the test a human's Approve asked for, on the version they approved: it decides what comes next (#380)
    approving = test and row["approving"] and test.get("for") == row["approving"] \
        and test.get("version", row["version"]) == row["version"]
    with db._write_lock, db.conn() as c:
        if test:
            c.execute("UPDATE routines SET last_test=? WHERE id=?", (json.dumps({**test, "at": db.now()}), rid))
        if approving:
            back = test.get("ok") and row["resume"] and row["state"] == "paused" and not cli.problem_with(_stored(row))
            c.execute("UPDATE routines SET approving=0" + (", state='enabled', failure=NULL, resume=0" if back else "")
                      + " WHERE id=? AND version=?", (rid, row["version"]))
        if isinstance(data.get("run"), dict):
            c.execute("UPDATE routines SET last_run=? WHERE id=?", (json.dumps({**data["run"], "at": db.now()}), rid))
        if data.get("failed"):
            # Only the version it ran: a definition changed since then is not failed by it.
            c.execute("UPDATE routines SET state='failed', failure=? WHERE id=? AND version=? AND state='enabled'",
                      (str(data["failed"])[:2000], rid, data.get("version", row["version"])))
    if approving:
        if not test.get("ok"):
            what = f"Test of version {row['version']} failed: {test.get('error') or 'see /routines#r=' + str(rid)} " \
                   "It stays paused; a fix is a new version."
        else:
            what = f"Test of version {row['version']} ok, " + ("found work. " if test.get("work") else "no work right now. ") \
                   + ("Enabled again." if back else "It stays paused until someone enables it on /routines.")
        _say(row, what, {})
    return get(rid)
