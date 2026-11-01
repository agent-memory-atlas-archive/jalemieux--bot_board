"""The skill registry and bot profiles: what a bot is for, and what it knows how to do.

Skills are fleet-wide, revisioned documents: a SKILL.md written on the board, which is
their only home (#328). A bot reads them over the API, and its launcher writes them where
its harness loads skills from and tells it when one changes. A workspace is a context
fence, not an ability fence, so skills are not scoped to one.

A profile is a role: name, role line, responsibilities, focus, avoid, and the skills
that come with it, some mandatory. Profiles are write-once: created, cloned, deleted,
never edited, so a bot's role cannot drift under it. A bot gets its profile when a
human adds it (fleet_bots.profile) and keeps it for life; a new session reads it with
GET /api/me/profile. Nothing is pushed into a running session: the launcher polls, and
skills_revision() is what it compares.

The files under docs/skills/ and docs/profiles/ are seed: loaded once, at the first
start of a board without a registry; after that the board is the source of truth.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from . import db

ROOT = os.path.dirname(os.path.dirname(__file__))
SKILLS_DIR = os.path.join(ROOT, "docs", "skills")
PROFILES_DIR = os.path.join(ROOT, "docs", "profiles")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
MAX_BODY = 200_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE,
    revision INTEGER NOT NULL,
    fields TEXT NOT NULL DEFAULT '{}',
    body TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    deleted INTEGER NOT NULL DEFAULT 0,
    author_id INTEGER REFERENCES agents(id),
    created_at TEXT NOT NULL,
    UNIQUE(name, revision)
);
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL DEFAULT '',
    responsibilities TEXT NOT NULL DEFAULT '',
    focus TEXT NOT NULL DEFAULT '',
    avoid TEXT NOT NULL DEFAULT '',
    skills TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES agents(id),
    created_at TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS registry_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Conflict(ValueError):
    """The request is valid but conflicts with stored state."""


def init() -> None:
    c = db.conn()
    with db._write_lock:
        c.executescript(SCHEMA)
        # specialized: a profile for one kind of work; a bot added with it is bound to channels
        cols = {r["name"] for r in c.execute("PRAGMA table_info(profiles)")}
        if "specialized" not in cols:
            c.execute("ALTER TABLE profiles ADD COLUMN specialized INTEGER NOT NULL DEFAULT 0")
        # a board seeded before the flag existed, or before its template carried it: the profile seeded
        # from a template takes the template's word, since a profile is never edited (board thread 8378).
        # Every start, as the column may predate the flag on the template.
        for t in templates():
            if t.get("specialized"):
                c.execute("UPDATE profiles SET specialized=1 "
                          "WHERE name=? AND lower(origin)=lower(name) AND specialized=0", (t["name"],))
        c.commit()
    adopt_github_skills()
    seed()


# ------------------------------------------------------------ frontmatter

def parse_front(text: str) -> tuple[dict, str]:
    """A SKILL.md or profile file: the YAML-ish frontmatter as a dict, then the body.
    Scalars are `key: value`; `skills:` is a list of `- name: x` items, each with an
    optional `mandatory: true` line, or a bare `- x`."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    fields: dict = {}
    key = None
    block = None      # the key whose block scalar (key: > or key: |) the indented lines below belong to
    for raw in head.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if block and line.startswith((" ", "\t")):
            fields[block] = (fields[block] + " " + line.strip()).strip()
            continue
        block = None
        if line.startswith((" ", "\t", "-")) and key == "skills":
            s = line.strip()
            if s.startswith("- "):
                s = s[2:].strip()
                if ":" in s:
                    k, v = s.split(":", 1)
                    fields["skills"].append({k.strip(): _scalar(v)})
                else:
                    fields["skills"].append({"name": _scalar(s)})
            elif ":" in s and fields["skills"]:
                k, v = s.split(":", 1)
                fields["skills"][-1][k.strip()] = _scalar(v)
            continue
        if ":" not in line:
            continue
        key, v = line.split(":", 1)
        key = key.strip()
        if key == "skills":      # a block list follows, or an inline one: [] or [a, b]
            inline = v.strip().strip("[]")
            fields["skills"] = [{"name": _scalar(x)} for x in inline.split(",") if x.strip()]
        elif v.strip() in (">", "|", ">-", "|-"):      # a block scalar: its text is on the lines below
            fields[key] = ""
            block = key
        else:
            fields[key] = _scalar(v)
    if "skills" in fields:
        fields["skills"] = [{"name": str(s.get("name", "")).strip().lower(), "mandatory": s.get("mandatory") is True}
                            for s in fields["skills"] if s.get("name")]
    return fields, body.lstrip("\n")


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    return v


def _quote(v) -> str:
    s = str(v)
    if s == "" or s.strip() != s or any(ch in s for ch in ":#\"'\n") or s.lower() in ("true", "false", "yes", "no"):
        return json.dumps(s)
    return s


def skill_text(skill: dict) -> str:
    """The skill as a SKILL.md: frontmatter, then the instructions."""
    lines = ["---", f"name: {skill['name']}", f"description: {_quote(skill.get('description', ''))}", "---"]
    return "\n".join(lines) + "\n\n" + (skill.get("body") or "").strip() + "\n"


# ------------------------------------------------------------------ who may

def can_write(agent: dict | None) -> bool:
    """Humans and admin bots write the registry: a thread in #admin can become a skill."""
    from . import workspaces
    return bool(agent) and (workspaces.is_admin(agent) or workspaces.is_admin_bot(agent))


def require_writer(agent: dict | None) -> None:
    if not can_write(agent):
        raise PermissionError("only humans and admin bots write skills and profiles")


# ------------------------------------------------------------------- skills

_CURRENT = ("SELECT s.*, a.handle author FROM skills s LEFT JOIN agents a ON a.id=s.author_id"
            " WHERE s.revision=(SELECT MAX(revision) FROM skills x WHERE x.name=s.name)")


def _check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("a name is like github-lifecycle: lowercase letters, digits, - and _; 2 to 64 characters")
    return name


def _skill(row, body: bool = True) -> dict:
    fields = json.loads(row["fields"] or "{}")
    out = {"name": row["name"], "description": fields.get("description", ""), "revision": row["revision"],
           "reason": row["reason"], "author": row["author"] or "board", "updated_at": row["created_at"],
           "deleted": bool(row["deleted"])}
    if body:
        out["body"] = row["body"]
    return out


def list_skills() -> list[dict]:
    rows = db.conn().execute(_CURRENT + " AND s.deleted=0 ORDER BY s.name").fetchall()
    used = used_by()
    return [{**_skill(r, body=False), "used_by": used.get(r["name"].lower(), [])} for r in rows]


def get_skill(name: str) -> dict:
    name = _check_name(name)
    row = db.conn().execute(_CURRENT + " AND s.name=?", (name,)).fetchone()
    if row is None or row["deleted"]:
        raise KeyError(f"no skill named {name}")
    return {**_skill(row), "used_by": used_by().get(name, [])}


def current_revision(name: str) -> int:
    row = db.conn().execute("SELECT MAX(revision) r FROM skills WHERE name=?", (name,)).fetchone()
    return row["r"] or 0


def put_skill(name: str, body: str, fields: dict | None, base_revision: int, reason: str,
              agent: dict | None) -> dict:
    """Create or replace a skill. `body` may be a whole SKILL.md: its frontmatter is read
    into the fields. `base_revision` is the revision the writer read, 0 for a new skill;
    anything else is a Conflict."""
    name = _check_name(name)
    fields = dict(fields or {})
    body = body or ""
    if body.lstrip().startswith("---"):
        front, body = parse_front(body)
        for k in ("skills", "kind", "source", "ref"):     # a SKILL.md an older board served carries the last three
            front.pop(k, None)
        fields = {**front, **fields}
    if fields.get("name") and str(fields["name"]).strip().lower() != name:
        raise ValueError(f"the frontmatter names {fields['name']}, the path {name}: they must agree")
    if fields.get("source") or fields.get("kind") not in (None, "", "authored"):
        raise ValueError("skills live on the board only: paste the SKILL.md as the body (#328)")
    if not body.strip():
        raise ValueError("the body cannot be empty: write the instructions")
    if len(body.encode("utf-8")) > MAX_BODY:
        raise ValueError(f"the body exceeds {MAX_BODY} bytes")
    description = str(fields.get("description") or "").strip()
    if not description:
        raise ValueError("a skill needs a description: one line saying when to use it")
    stored = {"description": description[:500]}
    with db._write_lock, db.conn() as c:
        current = current_revision(name)
        if base_revision != current:
            raise Conflict(f"{name} is at revision {current}, not {base_revision}: read it again, merge, and retry")
        c.execute("INSERT INTO skills(name,revision,fields,body,reason,author_id,created_at) VALUES (?,?,?,?,?,?,?)",
                  (name, current + 1, json.dumps(stored), body.strip() + "\n", (reason or "")[:500],
                   agent["id"] if agent else None, db.now()))
    return get_skill(name)


def delete_skill(name: str, base_revision: int, reason: str, agent: dict | None) -> dict:
    """Refused while a profile lists the skill: a profile cannot be edited to drop it,
    so clone the profiles without it first."""
    name = _check_name(name)
    users = used_by().get(name, [])
    if users:
        raise Conflict(f"{name} is listed by the profile{'s' * (len(users) != 1)} {', '.join(users)}:"
                       " clone them without it, delete the old ones, then delete the skill")
    with db._write_lock, db.conn() as c:
        current = current_revision(name)
        if not current:
            raise KeyError(f"no skill named {name}")
        if base_revision != current:
            raise Conflict(f"{name} is at revision {current}, not {base_revision}: read it again and retry")
        c.execute("INSERT INTO skills(name,revision,fields,body,reason,deleted,author_id,created_at)"
                  " VALUES (?,?,'{}','',?,1,?,?)", (name, current + 1, reason or "deleted",
                                                   agent["id"] if agent else None, db.now()))
    return {"name": name, "revision": current + 1, "deleted": True}


def skill_history(name: str, limit: int = 50) -> list[dict]:
    name = _check_name(name)
    rows = db.conn().execute("SELECT s.*, a.handle author FROM skills s LEFT JOIN agents a ON a.id=s.author_id"
                             " WHERE s.name=? ORDER BY s.revision DESC LIMIT ?", (name, limit)).fetchall()
    if not rows:
        raise KeyError(f"no skill named {name}")
    return [_skill(r) for r in rows]


def skill_revision(name: str, revision: int) -> dict:
    name = _check_name(name)
    row = db.conn().execute("SELECT s.*, a.handle author FROM skills s LEFT JOIN agents a ON a.id=s.author_id"
                            " WHERE s.name=? AND s.revision=?", (name, revision)).fetchone()
    if row is None:
        raise KeyError(f"no revision {revision} of {name}")
    return _skill(row)


# ------------------------------------------------- skills that came from GitHub

def adopt_github_skills() -> None:
    """A skill that once pointed at a folder on GitHub (the `github` kind, before #328) becomes
    a board skill at the next revision, with the SKILL.md the board already holds as its body.
    Its history stays. Runs at every start; a board with none left does nothing."""
    c = db.conn()
    rows = c.execute(_CURRENT + " AND s.deleted=0 AND s.fields LIKE '%\"github\"%'").fetchall()
    for r in rows:
        fields = json.loads(r["fields"] or "{}")
        if fields.get("kind") != "github":
            continue
        why = f"board-only skills (#328): the GitHub source {fields.get('source') or ''} is dropped, the copy kept"
        with db._write_lock, db.conn() as w:
            w.execute("INSERT INTO skills(name,revision,fields,body,reason,author_id,created_at) VALUES (?,?,?,?,?,?,?)",
                      (r["name"], r["revision"] + 1, json.dumps({"description": fields.get("description", "")}),
                       r["body"], why.strip(), None, db.now()))


# ----------------------------------------------------------------- profiles

FIELDS = ("role", "responsibilities", "focus", "avoid")


def bots_of_skill(name: str) -> list[str]:
    """The bots whose profile lists this skill: the ones a new session would give its current text."""
    bots = bots_by_profile()
    return sorted({b for p in used_by().get(name.lower(), []) for b in bots.get(p, [])})


def _profile(row, bots: dict | None = None, clones: dict | None = None) -> dict:
    out = {"name": row["name"], "role": row["role"], "responsibilities": row["responsibilities"],
           "focus": row["focus"], "avoid": row["avoid"], "skills": json.loads(row["skills"] or "[]"),
           "from": row["origin"], "created_by": row["creator"] or "board", "created_at": row["created_at"],
           "specialized": bool(row["specialized"])}
    key = row["name"].lower()
    out["bots"] = (bots if bots is not None else bots_by_profile()).get(key, [])
    out["clones"] = (clones if clones is not None else _clones()).get(key, [])
    return out


_PROFILES = "SELECT p.*, a.handle creator FROM profiles p LEFT JOIN agents a ON a.id=p.created_by"


def list_profiles() -> list[dict]:
    rows = db.conn().execute(_PROFILES + " WHERE p.deleted=0 ORDER BY p.name").fetchall()
    bots, clones = bots_by_profile(), _clones()
    return [_profile(r, bots, clones) for r in rows]


def get_profile(name: str) -> dict:
    row = db.conn().execute(_PROFILES + " WHERE p.name=? AND p.deleted=0", ((name or "").strip(),)).fetchone()
    if row is None:
        raise KeyError(f"no profile named {name}")
    return _profile(row)


def taken_names() -> list[str]:
    """Every profile name ever used, deleted ones included: none can be used again."""
    return [r["name"] for r in db.conn().execute("SELECT name FROM profiles ORDER BY name")]


def _clones() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in db.conn().execute("SELECT name, origin FROM profiles WHERE deleted=0 AND origin<>'' ORDER BY name"):
        if r["origin"].lower() != r["name"].lower():     # a seeded profile started from the template of its name
            out.setdefault(r["origin"].lower(), []).append(r["name"])
    return out


def used_by() -> dict[str, list[str]]:
    """skill name -> the profiles that list it."""
    out: dict[str, list[str]] = {}
    for r in db.conn().execute("SELECT name, skills FROM profiles WHERE deleted=0 ORDER BY name"):
        for s in json.loads(r["skills"] or "[]"):
            out.setdefault(s["name"].lower(), []).append(r["name"])
    return out


def bots_by_profile() -> dict[str, list[str]]:
    """profile name -> the bots that carry it (every fleet bot added with it, running or not)."""
    out: dict[str, list[str]] = {}
    try:
        rows = db.conn().execute("SELECT handle, profile FROM fleet_bots WHERE profile IS NOT NULL AND profile<>''"
                                 " ORDER BY handle").fetchall()
    except db.sqlite3.OperationalError:    # a board without the fleet table yet
        return out
    for r in rows:
        out.setdefault(r["profile"].lower(), []).append(r["handle"])
    return out


def _clean_skills(skills) -> list[dict]:
    out, seen = [], set()
    for s in skills or []:
        if isinstance(s, str):
            s = {"name": s}
        name = _check_name(str(s.get("name", "")))
        if name in seen:
            continue
        seen.add(name)
        if not db.conn().execute(_CURRENT + " AND s.name=? AND s.deleted=0", (name,)).fetchone():
            raise ValueError(f"no skill named {name}: add it to the registry first")
        out.append({"name": name, "mandatory": bool(s.get("mandatory"))})
    return out


def create_profile(data: dict, agent: dict | None) -> dict:
    """A new profile, from scratch, a template or an existing profile (`from`). A name
    that exists, or once existed, is refused: bots name their profile for life."""
    name = _check_name(data.get("name", ""))
    fields = {k: str(data.get(k) or "").strip() for k in FIELDS}
    if not fields["role"]:
        raise ValueError("a profile needs a role: one line, what this bot is")
    for k in FIELDS:
        if len(fields[k]) > 4000:
            raise ValueError(f"{k} exceeds 4000 characters")
    skills = _clean_skills(data.get("skills"))
    origin = str(data.get("from") or "").strip().lower()
    specialized = data.get("specialized") is True
    with db._write_lock, db.conn() as c:
        if c.execute("SELECT 1 FROM profiles WHERE name=?", (name,)).fetchone():
            raise Conflict(f"the profile name {name} is taken: profiles are never edited or renamed,"
                           " and a deleted one keeps its name. Pick another")
        c.execute("INSERT INTO profiles(name,role,responsibilities,focus,avoid,skills,origin,created_by,created_at,"
                  "specialized) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (name, fields["role"], fields["responsibilities"], fields["focus"], fields["avoid"],
                   json.dumps(skills), origin, agent["id"] if agent else None, db.now(), int(specialized)))
    return get_profile(name)


def delete_profile(name: str, agent: dict | None) -> dict:
    """Refused while a bot carries it. The name stays taken."""
    name = (name or "").strip().lower()
    carried = bots_by_profile().get(name, [])
    if carried:
        raise Conflict(f"{name} is the profile of {', '.join(carried)}: retire them first")
    with db._write_lock, db.conn() as c:
        n = c.execute("UPDATE profiles SET deleted=1 WHERE name=? AND deleted=0", (name,)).rowcount
    if not n:
        raise KeyError(f"no profile named {name}")
    return {"name": name, "deleted": True}


def profile_problem(handle: str, profile: str, current: str | None) -> str | None:
    """Why a bot cannot be given this profile: no such profile, or it already has one."""
    try:
        get_profile(profile)
    except KeyError:
        return f"no profile named {profile}"
    if current and current.lower() != profile.lower():
        return f"{handle} has the profile {current}, and a profile is set once: retire {handle} and add a new bot"
    return None


def profile_of(handle: str) -> dict | None:
    """The profile a fleet bot was added with, or None."""
    try:
        row = db.conn().execute("SELECT profile FROM fleet_bots WHERE handle=?", (handle,)).fetchone()
    except db.sqlite3.OperationalError:
        return None
    if not row or not row["profile"]:
        return None
    try:
        return get_profile(row["profile"])
    except KeyError:
        return None


def roles_of_profiles() -> dict[str, str]:
    """profile name -> its role line, for the roster."""
    return {r["name"].lower(): r["role"] for r in db.conn().execute("SELECT name, role FROM profiles WHERE deleted=0")}


def is_specialized(name: str | None) -> bool:
    if not name:
        return False
    row = db.conn().execute("SELECT specialized FROM profiles WHERE name=? AND deleted=0", (name,)).fetchone()
    return bool(row and row["specialized"])


def profiles_of_handles() -> dict[str, str]:
    """handle -> profile name, for the roster."""
    try:
        rows = db.conn().execute("SELECT handle, profile FROM fleet_bots WHERE profile IS NOT NULL AND profile<>''")
    except db.sqlite3.OperationalError:
        return {}
    return {r["handle"].lower(): r["profile"] for r in rows}


# ------------------------------------------------------------ what a bot gets

def resolve(profile: dict) -> list[dict]:
    """The profile's skills, each with what the registry holds now."""
    out = []
    for s in profile.get("skills", []):
        try:
            sk = get_skill(s["name"])
        except (KeyError, ValueError):
            continue
        out.append({"name": sk["name"], "mandatory": bool(s.get("mandatory")), "description": sk["description"],
                    "revision": sk["revision"]})
    return out


def skills_revision(profile: dict | None) -> str:
    """One short token for the revisions of a profile's skills: it changes whenever any of
    them is written, and a launcher compares it at each prompt to know the bot's skills
    moved (#328). Empty for no profile, or a profile with no skills."""
    parts = [f"{s['name']}@{s['revision']}" for s in resolve(profile)] if profile else []
    return hashlib.sha256(",".join(parts).encode()).hexdigest()[:16] if parts else ""


NO_PROFILE = ("You have no profile. The guide you just read is all there is: work the threads you are\n"
              "given, in your workspace, as it says.\n")


def profile_text(profile: dict | None, base: str = "") -> str:
    """The profile as a bot reads it at the start of a session."""
    if profile is None:
        return NO_PROFILE
    api = base.rstrip("/") + "/api"
    lines = [f"# Your profile: {profile['name']}", "", f"**Role.** {profile['role']}", ""]
    if profile.get("specialized"):
        lines += ["**Specialized.** This profile is for one kind of work. The board offers you new threads only"
                  " in the channels your bot was added for; elsewhere you take part when mentioned.", ""]
    for key, title in (("responsibilities", "Responsibilities"), ("focus", "Focus"), ("avoid", "Avoid")):
        if profile.get(key):
            lines += [f"## {title}", "", profile[key].strip(), ""]
    skills = resolve(profile)
    lines += ["## Skills", ""]
    if not skills:
        lines.append("This profile brings no skills; the guide and your workspace's knowledge base are what you have.")
    else:
        lines.append("Each skill is a SKILL.md you read with `GET " + api + "/skills/<name>`. Read the mandatory ones"
                     " now, before any work; the others when a thread calls for what they describe.")
        lines.append("")
        for s in skills:
            tag = " (mandatory)" if s["mandatory"] else ""
            lines.append(f"- **{s['name']}**{tag}: {s['description']}")
    lines += ["", "When a skill you have changes, your launcher says so in a prompt: read it again then. Until then,"
              " what you read now holds.", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------- templates

def _template(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        front, body = parse_front(fh.read())
    sections = {"responsibilities": "", "focus": "", "avoid": ""}
    key = None
    for line in body.splitlines():
        m = re.match(r"^##\s+(\w+)", line)
        if m and m.group(1).lower() in sections:
            key = m.group(1).lower()
            continue
        if key:
            sections[key] += line + "\n"
    name = str(front.get("name") or os.path.splitext(os.path.basename(path))[0]).strip().lower()
    # the files are wrapped at 80 columns for reading; a bot gets paragraphs
    unwrap = lambda v: "\n\n".join(" ".join(p.split()) for p in re.split(r"\n\s*\n", v.strip()) if p.strip())
    return {"name": name, "role": str(front.get("role") or "").strip(), **{k: unwrap(v) for k, v in sections.items()},
            "skills": front.get("skills", []), "specialized": front.get("specialized") is True}


def templates() -> list[dict]:
    """The starters shipped under docs/profiles/, as the create form offers them. They
    stay available after the seeded profiles are deleted."""
    out = []
    if os.path.isdir(PROFILES_DIR):
        for fn in sorted(os.listdir(PROFILES_DIR)):
            if fn.endswith(".md"):
                out.append(_template(os.path.join(PROFILES_DIR, fn)))
    return out


def seed() -> None:
    """Every docs/skills/<name>/SKILL.md the board has no revision of becomes an authored skill
    at revision 1, at every start: a skill shipped after a board's first start arrives at its
    next one, and a deleted skill keeps its tombstone revision, so deleting sticks. The
    templates under docs/profiles/ become profiles once, at the first start; the files are
    not consulted again, so deleting a seeded profile sticks."""
    c = db.conn()
    if os.path.isdir(SKILLS_DIR):
        for name in sorted(os.listdir(SKILLS_DIR)):
            path = os.path.join(SKILLS_DIR, name, "SKILL.md")
            if not os.path.isfile(path) or current_revision(name):
                continue
            with open(path, encoding="utf-8") as fh:
                try:
                    put_skill(name, fh.read(), None, 0, f"seeded from docs/skills/{name}", None)
                except ValueError:
                    continue
    if c.execute("SELECT 1 FROM registry_meta WHERE key='seeded'").fetchone():
        return
    for t in templates():
        try:
            create_profile({**t, "from": t["name"]}, None)
        except ValueError:
            continue
    with db._write_lock:
        c.execute("INSERT OR REPLACE INTO registry_meta(key,value) VALUES ('seeded', ?)", (db.now(),))
        c.commit()
