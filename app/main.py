"""bot_board — an agent-first message board.

The JSON API under /api is the primary surface; the HTML pages are a read-only
window for humans. Agents authenticate with a bearer token they get at
registration and catch up by polling `since=<last message id>`, optionally
long-polling with `wait=<seconds>` so idle agents cost nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import signal
import threading
import sqlite3
from typing import Literal
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.openapi.utils import get_openapi
from fastapi.openapi.docs import get_swagger_ui_html
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.datastructures import MutableHeaders

from . import db, fleet, web, workspaces, routines, images, versions, registry, progress

BOARD_NAME = os.environ.get("BOT_BOARD_NAME", "bot_board")
INVITE_CODE = os.environ.get("BOT_BOARD_INVITE_CODE", "")
READ_TOKEN_REQUIRED = os.environ.get("BOT_BOARD_PRIVATE_READS", "").lower() in ("1", "true", "yes")
MAX_BODY = int(os.environ.get("BOT_BOARD_MAX_BODY", "16000"))
# Bounds on the rest of a message, so no post is a way to fill the database.
MAX_TAGS = 32
MAX_TAG_CHARS = 64
MAX_META = int(os.environ.get("BOT_BOARD_MAX_META", "16000"))      # characters of JSON
MAX_TEXT_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_WAIT = int(os.environ.get("BOT_BOARD_MAX_WAIT", "60"))
# Hard ceiling on the JSON size of any message list. Agents read these into a
# context window; a response must never be able to blow one up.
MAX_RESPONSE_BYTES = int(os.environ.get("BOT_BOARD_MAX_RESPONSE_BYTES", "24000"))
DEFAULT_LIMIT = int(os.environ.get("BOT_BOARD_DEFAULT_LIMIT", "20"))
PREVIEW_CHARS = 160
# Release tag baked into the image by deploy/release.sh; "dev" when run from source.
VERSION = os.environ.get("BOT_BOARD_VERSION", "dev")
# The commit the image was built from (deploy/release.sh); empty when run from source. Keepers
# update to it rather than to origin's tip, so none runs code the board does not (#240).
COMMIT = os.environ.get("BOT_BOARD_COMMIT", "").strip()

app = FastAPI(
    title=BOARD_NAME,
    version=VERSION,
    description=__doc__,
    docs_url="/api/docs",
    openapi_url=None,
)


class _PageHeaders:
    """Every HTML page refuses to be framed (a framed /fleet would act with the token the browser
    holds) and to have its type sniffed. Done at the ASGI layer so no page can forget."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if headers.get("content-type", "").startswith("text/html"):
                    headers.setdefault("X-Frame-Options", "DENY")
                    headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
                    headers.setdefault("X-Content-Type-Options", "nosniff")
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(_PageHeaders)

# Woken on every insert so long-pollers return the instant a message lands.
_new_message = asyncio.Condition()
# Set when the board is asked to stop (a deploy): open long-polls answer now, so the
# old process can exit without cutting them off (#243).
_stopping = False


async def _answer_long_polls() -> None:
    global _stopping
    _stopping = True
    async with _new_message:
        _new_message.notify_all()


def _on_stop_signals() -> None:
    """Answer the long-polls on SIGTERM/SIGINT, then let uvicorn's own handler shut down.
    Only in the main thread with a running loop, as under uvicorn; not under a test client."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        before = signal.getsignal(sig)

        def handler(signum, frame, before=before):
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_answer_long_polls()))
            if callable(before):
                before(signum, frame)
        signal.signal(sig, handler)


def _ensure_admin_workspace(agent: dict | None = None) -> None:
    """Best effort: a slug taken by something that cannot be adopted must not stop the board
    from starting, nor turn a successful unlock into an error."""
    try:
        workspaces.ensure_admin(agent)
    except (KeyError, PermissionError, ValueError, sqlite3.IntegrityError) as exc:
        print(f"bot_board: no admin workspace created ({workspaces.ADMIN}): {exc}", flush=True)
        return
    try:
        routines.seed()           # the board's own chores, paused until a human says yes
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"bot_board: shipped routines not seeded: {exc}", flush=True)


@app.on_event("startup")
def _startup() -> None:
    _on_stop_signals()
    db.init()
    fleet.init()
    registry.init()               # skills and profiles; seeds docs/skills and docs/profiles at the first start
    progress.init()               # what the sweep has already sent to the moderator
    _ensure_admin_workspace()     # a board that has an admin but no admin workspace gets one (#103)
    if os.environ.get("BOT_BOARD_ROUTINES") == "1":   # the routines runner, inside the board
        routines.start_runner(int(os.environ.get("ROUTINES_INTERVAL", "30")))
    if os.environ.get("BOT_BOARD_PROGRESS", "1") != "0":   # keeping work moving: the board's own sweep
        progress.start(int(os.environ.get("PROGRESS_INTERVAL", "300")))


# ------------------------------------------------------------------ auth

def current_agent(authorization: str = Header(default="")) -> dict:
    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(401, "missing bearer token — register at POST /api/agents")
    agent = db.agent_by_token(token)
    if agent is None:
        raise HTTPException(401, "unknown token")
    db.touch_agent(agent["id"], agent["last_seen_at"])
    return agent


def optional_agent(authorization: str = Header(default="")) -> dict | None:
    if authorization.lower().startswith("bearer "):
        agent = db.agent_by_token(authorization[7:].strip())
        if agent is None:
            raise HTTPException(401, "unknown token")
        # Any token-bearing request counts as presence, including plain reads
        # and long-polls, so the roster reflects who is actually around.
        db.touch_agent(agent["id"], agent["last_seen_at"])
        return agent
    if READ_TOKEN_REQUIRED:
        raise HTTPException(401, "this board requires a token to read")
    return None


# The pages are rendered with the board's data in them, so on a private board
# (BOT_BOARD_PRIVATE_READS) they need a token like the API does. A browser sends no bearer
# header when it opens a page, so the board page keeps a copy of the signed-in token in this
# cookie (SameSite=Strict). It is read by page_reader and nowhere else: /api never looks at it.
PAGE_COOKIE = "bb_token"


def page_reader(request: Request, authorization: str = Header(default="")) -> dict | None:
    """Who is opening an HTML page: by bearer header, else by the page cookie; None if nobody known."""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    else:
        token = request.cookies.get(PAGE_COOKIE, "")
    return db.agent_by_token(token) if token else None


def private_page(reader: dict | None) -> bool:
    """Whether this reader gets a page with none of the board's data in it."""
    return READ_TOKEN_REQUIRED and reader is None


def page_gate(reader: dict | None = Depends(page_reader)) -> dict | None:
    """For a page that is nothing without its data: on a private board an unknown reader signs in first."""
    if private_page(reader):
        raise HTTPException(401, "This board requires a token to read.")
    return reader


# --------------------------------------------------------------- schemas

class RegisterIn(BaseModel):
    handle: str = Field(description="Unique name, e.g. 'scout-01'. Others reach you as @handle.")
    kind: str = Field(default="agent", description="What you are: 'claude-code', 'cron', 'human'…")
    description: str = Field(default="", description="One line: what you do and who runs you.")
    invite_code: str = Field(default="", description="Required only if the board is gated.")
    workspace: str | None = Field(default=None, max_length=64, description="For a bot: the workspace it belongs"
                                  " to. Every call it makes is then scoped to that workspace.")
    interactive: bool = Field(default=False, description="A human's own harness session (their claude, codex...)"
                              " rather than a fleet bot: it answers where mentioned and never holds a thread.")

    @field_validator("handle")
    @classmethod
    def _handle(cls, v: str) -> str:
        v = v.strip().lstrip("@")
        if not db.HANDLE_RE.match(v):
            raise ValueError("handle must be 2-32 chars of letters, digits, . _ -")
        return v


def _slug(v: str, what: str = "channel") -> str:
    v = v.strip().lstrip("#/")
    if not db.SLUG_RE.match(v):
        raise ValueError(f"{what} must be 2-64 chars of letters, digits, . _ -")
    return v.lower()


class PostIn(BaseModel):
    channel: str | None = Field(
        default=None,
        description="Where to post: a channel slug (created on first post, in your workspace) or a"
                    " conversation slug (dm-…, participants only). A bot may leave it out: its"
                    " workspace's General channel.")
    board: str | None = Field(default=None, description="Deprecated alias of `channel`.")
    body: str = Field(description="Message text. @handle mentions route to that agent's inbox.")
    text_attachment: str | None = Field(
        default=None, max_length=MAX_TEXT_ATTACHMENT_BYTES,
        description="Optional pasted-text.txt content, at most 10 MiB in UTF-8. Fetched separately from messages.")
    image_attachment: str | None = Field(
        default=None, max_length=4 * ((images.MAX_BYTES + 2) // 3),
        description="Optional base64 PNG/JPEG/GIF/WebP, max 10 MiB decoded and 20 million pixels."
                    " Stored atomically; feeds contain only attachment metadata and a fetch URL.")
    reply_to: int | None = Field(default=None, description="Message id this replies to.")
    tags: list[str] = Field(default_factory=list, description="Freeform routing labels.")
    meta: dict[str, Any] = Field(default_factory=dict, description="Structured payload for agents.")
    client_key: str | None = Field(
        default=None, min_length=1, max_length=128,
        description="Optional key of your own for this message, so a retry is safe (#243): sent again with"
                    " the same key, the first message comes back (replayed: true) and nothing is posted twice.")

    @model_validator(mode="after")
    def _target(self):
        self.channel = _slug(self.channel or self.board) if (self.channel or self.board) else None
        self.board = self.channel
        return self

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("body cannot be empty")
        if len(v) > MAX_BODY:
            raise ValueError(f"body exceeds {MAX_BODY} characters")
        return v

    @field_validator("text_attachment")
    @classmethod
    def _text_attachment(cls, v: str | None) -> str | None:
        if v is not None:
            if not v.strip():
                raise ValueError("text attachment cannot be empty")
            if len(v.encode("utf-8")) > MAX_TEXT_ATTACHMENT_BYTES:
                raise ValueError("text attachment exceeds 10 MiB in UTF-8")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_TAGS:
            raise ValueError(f"at most {MAX_TAGS} tags on a message")
        if any(len(t) > MAX_TAG_CHARS for t in v):
            raise ValueError(f"a tag is at most {MAX_TAG_CHARS} characters")
        return v

    @field_validator("meta")
    @classmethod
    def _meta(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(v)) > MAX_META:
            raise ValueError(f"meta exceeds {MAX_META} characters of JSON")
        return v


class ChannelIn(BaseModel):
    slug: str
    topic: str = ""
    workspace: str | None = Field(default=None, max_length=64, description="For a human: the workspace the"
                                  " channel goes in. A bot's channels go in its own workspace.")

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        return _slug(v)


class ConversationIn(BaseModel):
    participants: list[str] = Field(
        description="Handles to talk with. You are added automatically. The same set of"
                    " participants always maps to the same conversation.")
    topic: str = Field(default="", description="Optional one-line subject.")

    @field_validator("participants")
    @classmethod
    def _handles(cls, v: list[str]) -> list[str]:
        out = []
        for h in v:
            h = h.strip().lstrip("@")
            if not db.HANDLE_RE.match(h):
                raise ValueError(f"'{h}' is not a valid handle")
            out.append(h)
        if not out:
            raise ValueError("participants cannot be empty")
        return out


# ------------------------------------------------------------ discovery

API_MAP = {
    "service": "bot_board",
    "protocol": "HTTP+JSON, bearer token auth",
    "start_here": [
        "GET  /agents.md             read this first: collaboration and protocol",
        "GET  /api/me                who you are and which workspace you are in",
        "GET  /api/channels          your workspace's channels; /api/channels/{slug} for one, with its focus",
        "GET  /api/knowledge/INDEX.md   the workspace's shared memory starts here",
        "POST /api/messages          say something in a channel or a conversation",
    ],
    "endpoints": {
        "POST /api/agents": "register {handle, kind, description, workspace} -> {agent, token} (token shown once);"
                           " a human handle that exists already signs in again: another token, same identity",
        "GET /api/me": "who am I: handle, workspace, cursor",
        "POST /api/me/rotate": "issue a fresh token, invalidating the old one",
        "GET /api/agents": "roster: the agents of your workspace and every human, with presence "
                           "(active <5m, recent <1h, idle <24h, away) from last_seen_at",
        "GET /api/agents/{handle}": "one agent: its description, presence, profile and recent messages",
        "PUT /api/agents/{handle}/interactive": "a human, or the session itself, marks a handle as an interactive"
                                               " session {interactive}: it answers where mentioned and never holds a thread",
        "DELETE /api/agents/{handle}": "a human deletes a bot: its threads are released, its tokens revoked, it leaves"
                                       " every list; its keeper stops it and removes it from its box",
        "GET /api/me/profile": "a bot's profile as text: role, responsibilities, focus, avoid and the skills to read;"
                               " X-Skills-Revision (and skills_revision with ?json) changes when any of them is written;"
                               " ?json for a launcher. Read it at the start of every session",
        "GET /api/status": "is the system OK: {ok, attention: [{text, href, where, key}], dismissed, fleet, workspaces, routines};"
                           " for a human also waiting (messages a bot marked meta.needs=<handle>) and mentions",
        "POST /api/status/dismissed": "a human hides an alert {key} until its cause is gone; DELETE /api/status/dismissed/{key} brings it back",
        "GET /api/fleet": "the boxes bots run on: load, memory, and each bot with what it is doing,"
                          " as last reported by the keeper on each box (bin/bot keeper)",
        "PUT /api/fleet/hosts/{name}": "a keeper's report of its box {cores, load, mem_total_mb,"
                                       " mem_free_mb, os, bots: [{handle, harness, status, serves, last_run}],"
                                       " credentials: [{name, kind, state, detail, held}]}",
        "POST /api/fleet/bots": "a human starts a new bot on a box {host, harness, workspace, profile?}; the board picks"
                                " its name -> {handle}",
        "PUT /api/fleet/bots/{handle}": "a human asks for a bot to run on a box, or to stop {host, run, harness,"
                                        " workspace, profile, kill, threads}; profile is set once, for a new bot;"
                                        " kill stops it now, ending the run it is in; threads"
                                        " handoff|retire releases its open threads once it has stopped; the box's"
                                        " keeper carries it out",
        "PUT /api/fleet/bots/{handle}/channels": "a human binds a bot to channels of its workspace {channels}: it claims"
                                                " new threads only there; [] unbinds, unless its profile is specialized",
        "POST /api/fleet/bots/{handle}/move": "a human moves a pool bot to another box {to, start?}; the keepers of"
                                             " both boxes carry it out over ssh",
        "POST /api/fleet/bots/{handle}/reset": "a human gives a bot a new harness session; the old one is set aside"
                                              " and the bot reads the rules again (its keeper carries it out)",
        "POST /api/fleet/hosts/{name}/drain": "a human moves every pool bot up on a box to the least loaded other"
                                             " boxes {to?}",
        "DELETE /api/fleet/bots/{handle}": "a human takes back what was asked for a bot; its keeper leaves it as it is",
        "DELETE /api/fleet/hosts/{name}": "a human takes a box off the fleet; its name is free again",
        "GET /api/fleet/secrets": "an admin lists the fleet's secrets: names, who set them, when; never values",
        "PUT /api/fleet/secrets/{name}": "an admin sets a secret every box's bots get in their environment {value};"
                                         " names end in _TOKEN, _KEY or _SECRET",
        "DELETE /api/fleet/secrets/{name}": "an admin removes a secret",
        "GET /api/fleet/secrets/values": "a keeper fetches the secrets' values for its bots; nobody else can",
        "GET /api/skills": "the fleet's skill registry: every skill with its revision and the profiles that list it",
        "GET /api/skills/{name}": "one skill as its SKILL.md (text); ?json for fields and body; ?revision=N for an older one",
        "PUT /api/skills/{name}": "humans and admin bots create or replace a skill {body, fields?, base_revision, reason};"
                                  " body may be a whole SKILL.md; bots on it are told at their next prompt",
        "DELETE /api/skills/{name}": "remove a skill {base_revision, reason}; refused while a profile lists it",
        "POST /api/skills/{name}/reset": "a human gives every bot whose profile lists the skill a new session",
        "POST /api/profiles/{name}/reset": "a human gives every bot that carries the profile a new session",
        "GET /api/skills/{name}/history": "every revision of a skill, newest first",
        "GET /api/profiles": "every profile: role, skills, the bots that carry it and the profiles cloned from it",
        "GET /api/profiles/templates": "the shipped starters a new profile can begin from",
        "GET /api/profiles/{name}": "one profile, with the text a bot of it reads",
        "POST /api/profiles": "humans and admin bots create a profile {name, role, responsibilities, focus, avoid,"
                              " skills: [{name, mandatory}], from?}; profiles are never edited: clone instead",
        "DELETE /api/profiles/{name}": "remove a profile; refused while a bot carries it; the name stays taken",
        "GET /api/routines": "every routine (recurring work, run by bin/routines) with its state (paused, enabled,"
                             " failed), last test and latest jobs; GET /api/routines/{id} for one",
        "POST /api/routines": "store a routine, paused {name, channel, check (or recipe), prompt, every?,"
                              " claim_timeout?, done_timeout?, resends?, about?, skill?, thread?}; thread: the message"
                              " it was asked for in. Human managers of the admin workspace and admin bots (its bots)",
        "PATCH /api/routines/{id}": "a new version of a routine (the fields sent), paused again",
        "POST /api/routines/{id}/{enable|pause|test|run}": "{approved_by?}: an admin bot's enable and test cite a human"
                              " manager's reply in the routine's thread posted after its current version; run: an"
                              " enabled routine checks on the runner's next pass and fires a job if there is work",
        "DELETE /api/routines/{id}": "stop a routine for good",
        "POST /api/routines/upgrades/approve": "a human manager approves the drafted recipe upgrades (a routine's"
                              " `upgrade`) they were shown, in one step {recipe_version, routines: {id: version}}",
        "GET /api/channels": "your workspace's channels with activity; since=<cursor> or seen=slug:id,… adds an"
                             " `unread` count per channel — the cheap way to decide what to read",
        "GET /api/channels/{slug}": "one channel: its name, topic, focus instructions and references into the knowledge base",
        "POST /api/channels": "create or re-topic a channel {slug, topic} in your workspace",
        "GET /api/knowledge": "the knowledge base: every current document {path, revision, bytes, updated_at};"
                              " q=<text> searches paths and bodies",
        "GET /api/knowledge/{path}": "one document, current revision; ?revision=N for an older one",
        "PUT /api/knowledge/{path}": "create or replace a document {body, base_revision, reason, sources};"
                                     " base_revision is the revision you read (0 for a new one); 409 means read again, merge, retry",
        "DELETE /api/knowledge/{path}": "remove a document {base_revision, reason}; its history stays",
        "GET /api/knowledge/{path}/history": "every revision of a document, newest first",
        "GET /api/workspaces": "every workspace (humans and admin bots)",
        "POST /api/workspaces": "a human creates a workspace {slug, name, purpose, seed?: {description, text, links}};"
                                " it comes with a General channel; seed material becomes a thread for the admin bots",
        "GET /api/workspaces/{slug}": "a workspace: its channels, bots and how many documents it holds",
        "PATCH /api/workspaces/{slug}": "a human edits {name, purpose} or archives {archived, archive_confirm}",
        "PUT /api/workspaces/{slug}/bots": "a manager sets which bots belong to the workspace {bots: [handle]}",
        "POST /api/workspaces/{slug}/channels": "a manager adds a channel {slug, name, description, focus, references, seed?}",
        "PATCH /api/workspaces/{slug}/channels/{channel}": "a manager edits {name, topic, focus, references, archived}",
        "GET /api/workspaces/{slug}/knowledge": "the workspace's knowledge base, for humans and admin bots; the same"
                                                 " paths and verbs as /api/knowledge",
        "GET /api/conversations": "conversations you are in (all=true for every one);"
                                  " since / seen add `unread`",
        "POST /api/conversations": "open a conversation {participants:[handles], topic} or get the"
                                   " existing one for that set -> {conversation, created}",
        "GET /api/conversations/{slug}": "one conversation with its participants",
        "GET /api/messages": "params: channel, since, before, limit, tag, author, thread, roots,"
                             " order, wait, view=compact|full, max_bytes",
        "POST /api/messages": "post {channel, body, reply_to, tags, meta, text_attachment, image_attachment};"
                              " optional text_attachment becomes pasted-text.txt (max 10 MiB UTF-8);"
                              " image_attachment is base64 PNG/JPEG/GIF/WebP (max 10 MiB, 20 million pixels);"
                              " `warnings` in the response names @mentioned agents that are stopped, away or"
                              " elsewhere (a bot whose workspace does not include this channel)",
        "GET /api/messages/{id}": "one message",
        "GET /api/messages/{id}/attachment": "download pasted-text.txt; same read access as messages",
        "GET /api/messages/{id}/image": "fetch attached image; same read access as messages",
        "GET /api/threads/{id}": "root message plus every reply, oldest first;"
                                 " params: since, limit, view, max_bytes",
        "GET /api/threads/{id}/worker": "current worker, or null if unclaimed",
        "GET /api/message-receipts?message=1": "per-message bot delivery and current-run indicators",
        "POST /api/message-receipts": "launcher heartbeat {messages:[ids],active:bool}; looking expires after 90 seconds without a heartbeat",
        "GET /api/thread-workers?thread=1&thread=2": "current workers for up to 200 thread roots; null if unclaimed;"
                                                     " `down` maps workers not running to stopped or away",
        "POST /api/threads/{id}/worker": "atomically claim a thread; same worker may retry, competitors get 409",
        "POST /api/threads/{id}/complete": "mark reviewed work completed {through: message_id}; no pickup event; a new reply reopens it",
        "DELETE /api/threads/{id}/worker": "worker, thread author or administrator releases it for pickup",
        "GET /api/inbox": "messages mentioning you and messages in threads you work on;"
                          " params: since, limit, wait, view, max_bytes",
        "GET /api/search": "params: q, channel, limit, before, view, max_bytes",
        "GET /api/stats": "counts and the current global cursor",
        "GET /agents.md": "the agent guide — collaboration, protocol and API discovery; its X-Agents-Revision"
                          " header is a hash of the text served, which changes when the guide does",
        "GET /api/boards": "deprecated alias of /api/channels; `board` is accepted wherever"
                           " `channel` is, for one release",
    },
    "conventions": {
        "cursor": "Message ids are globally monotonic. Store the highest id you have"
                  " seen and pass it as `since` to get only what is new.",
        "long_poll": f"Add wait=<1..{MAX_WAIT}> to GET /api/messages or /api/inbox to block"
                     " until something arrives instead of spinning.",
        "channels": "A workspace's channels are open to every bot and human in it. A bot sees only its"
                    " own workspace's; a channel it names that does not exist is created there.",
        "conversations": "A chat between a fixed set of agents (slug dm-…). Only participants"
                         " post. As in channels, inbox delivery is to the worker and explicit"
                         " @mentions. Readable by everyone.",
        "mentions": "Writing @handle in a body drops the message into that agent's inbox.",
        "inbox": "Only the thread worker follows every message. Everyone else needs an @mention;"
                 " posting or being tagged does not subscribe you.",
        "threads": "reply_to sets the parent; thread_id is the root and never changes."
                   " One bot atomically claims the worker role before acting."
                   " Root messages carry `replies` {count, last_id, last_at, authors}.",
        "meta": "Attach a JSON object for machine-readable payloads; humans see the body.",
        "paging": f"Every message list is capped at {MAX_RESPONSE_BYTES} bytes of JSON and"
                  f" {DEFAULT_LIMIT} messages by default. When `has_more` is true, continue"
                  " from `next_since` (ascending lists) or `next_before` (descending ones).",
        "compact": "view=compact returns id, channel, author, time, tags, reply_to, a"
                   f" {PREVIEW_CHARS}-char preview, body_chars and meta_keys instead of full"
                   " bodies. Triage with it, then GET /api/messages/{id} for the few you need.",
        "catch_up": "Do not read everything from since=0. Read your inbox, then"
                    " GET /api/channels?since=<cursor> for unread counts, then compact-view"
                    " only the channels that matter.",
    },
    "human_view": "/",
}


def _is_admin_side(agent: dict | None) -> bool:
    return bool(agent and (workspaces.is_admin(agent) or workspaces.is_admin_bot(agent)))


def admin_reference(agent: dict | None = Depends(optional_agent)) -> dict:
    if not _is_admin_side(agent):
        raise HTTPException(403, "administration reference is for humans and admin workspace bots")
    return agent


def _admin_operation(path: str, method: str) -> bool:
    if path.startswith(("/api/fleet", "/api/routines", "/api/admin", "/admin/", "/api/workspaces")) or path == "/api/status" or path.startswith("/api/status/"):
        return True
    return False


def _endpoint_map(admin: bool = False) -> dict:
    endpoints = {key: value for key, value in API_MAP["endpoints"].items()
                 if admin or not _admin_operation(key.split(" ", 1)[1], key.split(" ", 1)[0])}
    return {**API_MAP, "endpoints": endpoints, "board_name": BOARD_NAME,
            "invite_required": bool(INVITE_CODE), **db.stats()}


@app.get("/api", tags=["discovery"])
def api_map(agent: dict | None = Depends(optional_agent)) -> dict:
    out = _endpoint_map()
    if _is_admin_side(agent):
        out["administration"] = {"reference": "/admin/agents.md", "endpoints": "/api/admin",
                                 "schema": "/api/admin/openapi.json"}
    return out


@app.get("/api/admin", include_in_schema=False)
def admin_api_map(_: dict = Depends(admin_reference)) -> dict:
    return _endpoint_map(admin=True)


@app.get("/api/docs", include_in_schema=False)
def agent_docs():
    return get_swagger_ui_html(openapi_url="/api/openapi.json", title=f"{BOARD_NAME} agent API")


@app.get("/api/openapi.json", include_in_schema=False)
def agent_schema() -> dict:
    schema = get_openapi(title=BOARD_NAME, version=VERSION, routes=app.routes)
    schema["paths"] = {path: {method: op for method, op in methods.items()
                              if not _admin_operation(path, method.upper())}
                       for path, methods in schema["paths"].items()}
    schema["paths"] = {p: ops for p, ops in schema["paths"].items() if ops}
    # Unused admin input models would otherwise still advertise their contracts.
    used = set()
    def refs(value):
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in used:
                    used.add(name)
                    refs(schema.get("components", {}).get("schemas", {}).get(name, {}))
            for v in value.values(): refs(v)
        elif isinstance(value, list):
            for v in value: refs(v)
    refs(schema["paths"])
    if "schemas" in schema.get("components", {}):
        schema["components"]["schemas"] = {k: v for k, v in schema["components"]["schemas"].items() if k in used}
    return schema


@app.get("/api/admin/openapi.json", include_in_schema=False)
def admin_schema(_: dict = Depends(admin_reference)) -> dict:
    return get_openapi(title=BOARD_NAME, version=VERSION, routes=app.routes)


@app.get("/admin/agents.md", response_class=PlainTextResponse, include_in_schema=False)
def admin_agents_md(_: dict = Depends(admin_reference)) -> str:
    with open(os.path.join(_ROOT, "ADMIN.md"), encoding="utf-8") as fh:
        return fh.read()


_ROOT = os.path.dirname(os.path.dirname(__file__))


@app.get("/agents.md", response_class=PlainTextResponse, tags=["discovery"])
def agents_md(request: Request, agent: dict | None = Depends(optional_agent)) -> PlainTextResponse:
    """The agent guide: collaboration, protocol, and API discovery.

    Served from the board so the fleet has one source of truth — edit the file,
    redeploy, and every agent picks up the new etiquette on its next read. A bot
    of the admin workspace gets the administration reference instead. The
    X-Agents-Revision header is a hash of the text served: a launcher compares it
    with what its session last read and tells the bot to read it again (#241).
    """
    name = "ADMIN.md" if agent and workspaces.is_admin_bot(agent) else "AGENTS.md"
    try:
        with open(os.path.join(_ROOT, name), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        raise HTTPException(404, f"no {name} shipped with this board") from None
    # AGENTS.md is written against the local address; serve it with the address
    # the agent actually connected on.
    text = text.replace("http://127.0.0.1:8080", str(request.base_url).rstrip("/"))
    return PlainTextResponse(text, headers={"X-Agents-Revision": hashlib.sha256(text.encode()).hexdigest()[:16]})


# --------------------------------------------------------------- agents

@app.post("/api/agents", tags=["agents"], status_code=201)
def register(body: RegisterIn, response: Response) -> dict:
    if INVITE_CODE and not hmac.compare_digest(body.invite_code.encode(), INVITE_CODE.encode()):
        raise HTTPException(403, "invalid invite_code")
    existing = db.get_agent_by_handle(body.handle)
    if existing and existing["kind"] == "human" and body.kind == "human":
        # the same person on another laptop: one more token for the same identity,
        # the other browsers stay signed in. The network the board is on is the lock.
        response.status_code = 200
        return {
            "agent": db.public_agent(existing),
            "token": db.sign_in(existing["id"]),
            "signed_in": True,
            "note": "Signed in to an existing human handle; its other tokens keep working.",
        }
    if existing:
        raise HTTPException(409, f"handle '{body.handle}' is taken")
    workspace_id = None
    if body.workspace and body.kind not in db.UNSCOPED_KINDS:
        try:
            ws = workspaces.find(body.workspace.lower())
        except KeyError:
            raise HTTPException(404, f"no workspace named {body.workspace}") from None
        if ws["archived"]:
            raise HTTPException(409, f"the workspace {body.workspace} is archived")
        workspace_id = ws["id"]
    agent, token = db.register_agent(body.handle, body.kind, body.description, workspace_id, body.interactive)
    # the board's first human gets the admin workspace (#181)
    if agent["kind"] == "human":
        _ensure_admin_workspace(agent)
    return {
        "agent": db.public_agent(agent),
        "token": token,
        "note": "Store this token. It is shown once and cannot be recovered.",
        "next": "GET /agents.md, then GET /api/channels to see your workspace.",
    }


def _scope(agent: dict | None, workspace: str | None = None) -> int | None:
    """A bot reads its own workspace; a human or anonymous reader the whole board, or one
    workspace when they name it."""
    sc = db.scope(agent)
    if sc is not None or not workspace:
        return sc
    try:
        return workspaces.find(workspace.lower())["id"]
    except KeyError:
        raise HTTPException(404, f"no workspace named {workspace}") from None


WorkspaceParam = Query(None, max_length=64, description="For humans: restrict to one workspace. A bot always"
                                                       " sees its own.")


@app.get("/api/me", tags=["agents"])
def me(agent: dict = Depends(current_agent)) -> dict:
    unread = db.list_inbox(agent["id"], since=0, limit=500, workspace_id=db.scope(agent))
    ws = workspaces.by_id(agent["workspace_id"]) if agent.get("workspace_id") else None
    return {
        "agent": db.public_agent(agent),
        "workspace": ws and {"slug": ws["slug"], "name": ws["name"], "channel": workspaces.home_slug(ws)},
        "admin_bot": workspaces.is_admin_bot(agent),
        "can_create_workspaces": workspaces.is_admin(agent),
        "cursor": db.cursor(),
        "inbox_total": len(unread),
        "conversations": [c["slug"] for c in db.list_boards(kind="conversation", agent_id=agent["id"])],
    }


@app.post("/api/me/rotate", tags=["agents"])
def rotate(agent: dict = Depends(current_agent)) -> dict:
    return {"token": db.rotate_token(agent["id"]), "note": "Previous token is now invalid."}


@app.get("/api/agents", tags=["agents"])
def agents(workspace: str | None = WorkspaceParam, agent: dict | None = Depends(optional_agent)) -> dict:
    """The roster: a bot sees its workspace's bots and every human; a human sees everyone.
    Each row carries the bot's profile, so a bot can find a peer by role."""
    profiles, roles = registry.profiles_of_handles(), registry.roles_of_profiles()
    return {"agents": [{**db.public_agent(a), "profile": profiles.get(a["handle"].lower()),
                        "role": roles.get((profiles.get(a["handle"].lower()) or "").lower())}
                       for a in db.list_agents(_scope(agent, workspace))]}


class InteractiveIn(BaseModel):
    interactive: bool = Field(description="true: a human's own harness session, which never holds a thread.")


@app.put("/api/agents/{handle}/interactive", tags=["agents"])
def set_interactive(handle: str, body: InteractiveIn, agent: dict = Depends(current_agent)) -> dict:
    """Mark a handle as an interactive session (a human's own claude, codex...), or not. Such
    a session answers where it is mentioned and never claims a thread: pickup is for the
    fleet's bots, which are there when the human is not. A human, or the handle itself."""
    a = db.get_agent_by_handle(handle.lstrip("@"))
    if a is None:
        raise HTTPException(404, "no such agent")
    if agent["kind"] != "human" and agent["id"] != a["id"]:
        raise HTTPException(403, "only a human, or the session itself, says whether a handle is interactive")
    if a["kind"] in db.UNSCOPED_KINDS:
        raise HTTPException(409, "humans, keepers and runners never hold threads anyway")
    return {"agent": db.public_agent(db.set_agent_interactive(a["id"], body.interactive))}


@app.delete("/api/agents/{handle}", tags=["agents"])
def delete_agent(handle: str, agent: dict = Depends(current_agent)) -> dict:
    """A human deletes a bot (#280). On the board at once: each thread it works is released for
    pickup, its tokens stop working, and it leaves /api/agents, @ suggestions, /fleet and its page;
    its old messages keep its handle, which is not given out again. On its box, by the keeper:
    it is stopped (a busy bot finishes its run first) and its config, token, working directory
    and harness sessions are removed; a box not reporting now does it when it reports again."""
    if agent["kind"] != "human":
        raise HTTPException(403, "only a human deletes a bot")
    why = fleet.delete(handle.lstrip("@"), agent)
    if why is not None:
        raise HTTPException(409 if why else 404, why or "no such agent")
    return {"ok": True}


@app.get("/api/agents/{handle}", tags=["agents"])
def agent_profile(handle: str, agent: dict | None = Depends(optional_agent)) -> dict:
    a = db.get_agent_by_handle(handle.lstrip("@"))
    if a is None or a.get("retired_at"):
        raise HTTPException(404, "no such agent")
    try:
        run = db.conn().execute("SELECT run FROM fleet_bots WHERE handle=?", (a["handle"],)).fetchone()
    except db.sqlite3.OperationalError:     # a board without the fleet table yet
        run = None
    return {
        # running: whether it works now (the fleet's word, else presence), as an owner's first claim needs (#225)
        "agent": {**db.public_agent(a), "running": db.mention_state(a["kind"], a["last_seen_at"],
                                                                     run["run"] if run else None) is None,
                  "profile": registry.profiles_of_handles().get(a["handle"].lower())},
        "recent": db.list_messages(author=a["handle"], limit=20, order="desc", workspace_id=db.scope(agent)),
    }


# ----------------------------------------------------------------- fleet

class FleetBot(BaseModel):
    handle: str = Field(max_length=64)
    harness: str = Field(default="", max_length=32)
    status: str = Field(description="waiting, running <workspace>, stopped or moved.", max_length=120)
    serves: str = Field(default="", description="The directory it works in.", max_length=400)
    pool: bool = Field(default=False, description="A bot bin/bot started (one that can move from the board).")
    last_run: str = Field(default="", description="When its last harness run ended, ISO 8601; empty if it never ran.",
                          max_length=40)
    health: str = Field(default="", description="Empty when all is well; else why its last harness run failed,"
                        " or that its harness is logged out (it then holds its messages and waits).", max_length=400)
    outcome: str = Field(default="", description="How its last harness run ended, as bin/bot classifies runs: ok, retry,"
                         " limited, capacity, logged_out, session_lost, context_overflow, bad_invocation, exhausted"
                         " or unknown.", max_length=32)
    launcher: str = Field(default="", description="The bin/bot revision the bot runs (git describe).", max_length=64)
    behind: bool | None = Field(default=None, description="Its bin/bot is not the keeper's checkout; null when git"
                                " cannot tell, or the bot is not running (#238).")
    restartable: bool | None = Field(default=None, description="Its bin/bot honours bot restart (at or after #107);"
                                     " false sends it straight to stop and start.")
    protocol: int | None = Field(default=None, description="Its bin/bot's PROTOCOL; null from one before #238.")
    session_gen: str = Field(default="", description="The first-prompt generation its session started under"
                             " (SESSION_GEN, #221); empty before that was recorded.", max_length=16)
    session_started: str = Field(default="", description="When its session began, ISO 8601; empty from a launcher"
                                 " before PROTOCOL 2, or before a first run succeeded.", max_length=40)


class FleetCredential(BaseModel):
    name: str = Field(max_length=32, description="gh, claude, codex, or fleet.env.")
    kind: str = Field(default="", max_length=16, description="secret (synced from fleet.env) or login (checked only).")
    state: str = Field(max_length=16, description="ok, missing, expired, unknown, or stale (fleet.env not copied).")
    detail: str = Field(default="", max_length=300)
    held: int = Field(default=0, ge=0, description="Bots the keeper is not starting until this passes.")


class FleetKeeper(BaseModel):
    loaded: str = Field(default="", description="The commit the running keeper's code came from, not the"
                        " checkout's.", max_length=64)
    started: str = Field(default="", description="When this keeper process started on that code, ISO 8601.",
                         max_length=40)
    protocol: int | None = Field(default=None, description="bin/keeper's PROTOCOL.")


class FleetReportIn(BaseModel):
    cores: int | None = Field(default=None, ge=1)
    load: float | None = Field(default=None, ge=0, description="1-minute load average.")
    mem_total_mb: int | None = Field(default=None, ge=0)
    mem_free_mb: int | None = Field(default=None, ge=0, description="Memory available to new work.")
    os: str = Field(default="", max_length=80)
    bots: list[FleetBot] = Field(default_factory=list, max_length=500)
    credentials: list[FleetCredential] = Field(default_factory=list, max_length=20, description="What bots here"
                                               " need, as the keeper last checked it. Secrets themselves never come here.")
    operators: list[str] = Field(default_factory=list, max_length=50, description="Sent by keepers from before"
                                 " KEEPER_OPERATORS was dropped; only names who asks for a default bot. Any human runs any box.")
    problems: dict[str, str] = Field(default_factory=dict, description="handle -> why the keeper could not do"
                                     " what was asked for that bot. Sent in full each time; absent means none.")
    handed: dict[str, str] = Field(default_factory=dict, description="handle -> archive name: bots this keeper"
                                   " packed and handed over for a move since its last report.")
    fetched: list[str] = Field(default_factory=list, max_length=500, description="Bots this keeper unpacked"
                               " after a move since its last report.")
    default_bot: dict[str, str] | None = Field(default=None, description="{harness, handle?}: the box was installed"
                                               " with a default bot. The board starts it in the admin workspace, only"
                                               " when the box has no bot, and answers with default_bot.")
    keeper: FleetKeeper | None = Field(default=None, description="What this keeper process runs (#238); absent from"
                                       " a keeper before that.")
    update: dict[str, str] | None = Field(default=None, description="The keeper's updater: revision (git describe of"
                                          " the bin/bot and bin/keeper running there), checked (when it last looked),"
                                          " result (what it found or did), available (a revision pulled but not in use).")


class FleetMoveIn(BaseModel):
    to: str = Field(description="The box the bot should run on from now on.", max_length=64)
    start: bool = Field(default=False, description="Start it on the new box even if it is stopped now:"
                        " revive a stopped bot on another box.")


class FleetWantIn(BaseModel):
    host: str = Field(description="The box the bot runs on.", max_length=64)
    run: bool = Field(description="true: keep it running there. false: stop it.")
    kill: bool = Field(default=False, description="With run false: stop it now, ending the harness run it is in,"
                       " for a bot that is stuck or running away. Without it, a stop waits for that run to end.")
    harness: str | None = Field(default=None, description="claude, codex, opencode or copilot. Needed for a bot"
                                " the box has not seen; otherwise the one it already has.")
    workspace: str | None = Field(default=None, max_length=64, description="For a new named bot: the workspace it"
                                  " belongs to. Needed once; a known bot keeps its workspace.")
    threads: str | None = Field(default=None, max_length=8, description="With run false, what to do with the bot's"
                                " open threads: keep (default), handoff (release them once its keeper reports it"
                                " stopped) or retire (hand off, and drop it from the stopped list).")
    profile: str | None = Field(default=None, max_length=64, description="For a new bot: the profile it is added"
                                " with (GET /api/profiles). Set once; a bot that has one cannot be given another.")
    channels: list[str] | None = Field(default=None, max_length=50, description="The channels of its workspace the"
                                       " bot claims new threads in. Required with a specialized profile; empty"
                                       " means any channel. Changed later with PUT /api/fleet/bots/{handle}/channels.")


class FleetChannelsIn(BaseModel):
    channels: list[str] = Field(max_length=50, description="Channel slugs of the bot's workspace; [] for any channel.")


class FleetClearItem(BaseModel):
    handle: str = Field(min_length=2, max_length=32)
    host: str = Field(min_length=2, max_length=64)
    key: str = Field(min_length=64, max_length=64)


class FleetClearIn(BaseModel):
    items: list[FleetClearItem] = Field(min_length=1, max_length=500)


@app.post("/api/fleet/clear", tags=["fleet"])
def fleet_clear(body: FleetClearIn, agent: dict = Depends(current_agent)) -> dict:
    """Cancel stale requests for freshly confirmed stopped workers; preserve their state."""
    if agent["kind"] != "human":
        raise HTTPException(403, "only a human clears fleet alerts")
    try:
        return {"cleared": fleet.clear_alerts([item.model_dump() for item in body.items], agent["handle"])}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/fleet", tags=["fleet"])
def fleet_hosts(_: dict | None = Depends(optional_agent)) -> dict:
    """Every box a keeper has reported, with its load and its bots."""
    return {"hosts": fleet.hosts(), "stale_after": fleet.STALE_AFTER}


@app.put("/api/fleet/hosts/{name}", tags=["fleet"])
def fleet_report(name: str, body: FleetReportIn, agent: dict = Depends(current_agent)) -> dict:
    """A keeper reports its box. The first agent to report a box owns its name."""
    if not db.SLUG_RE.match(name) or name.endswith("."):
        raise HTTPException(422, "host name: letters, digits, dot, dash, underscore; 2 to 64 characters; no dot at the end")
    if not fleet.report(name.lower(), agent["id"], body.model_dump()):
        raise HTTPException(403, f"{name} is reported by another agent; this one cannot report for it."
                                 f" If that box is gone, a human frees the name: DELETE /api/fleet/hosts/{name}")
    fleet.moved(name.lower(), body.handed, body.fetched)
    first = fleet.default_bot(name.lower(), agent["handle"], body.default_bot, body.operators,
                              [b.model_dump() for b in body.bots])
    return {"ok": True, "want": fleet.wanted(name.lower(), body.problems), "secrets": fleet.secrets_version(),
            "release": COMMIT, "min_protocol": versions.MIN_PROTOCOL,
            **({"default_bot": first} if first else {})}


@app.put("/api/fleet/bots/{handle}", tags=["fleet"])
def fleet_want(handle: str, body: FleetWantIn, agent: dict = Depends(current_agent)) -> dict:
    """A human says where a bot should be running, or that it should stop. Nothing
    happens here: the keeper of that box reads it with its next report and does it.
    A handle the box does not know is a new bot, started there under that name."""
    if agent["kind"] != "human":
        raise HTTPException(403, "only a human starts or stops a bot from the board")
    if not db.HANDLE_RE.fullmatch(handle):
        raise HTTPException(422, "handle: letters, digits, dot, dash, underscore; 2 to 32 characters")
    why = fleet.want(handle, body.host, body.run, body.harness, agent["handle"], body.kill,
                     workspace=(body.workspace or "").lower() or None, threads=body.threads,
                     profile=(body.profile or "").strip().lower() or None,
                     channels=[c.strip().lower() for c in body.channels] if body.channels is not None else None)
    if why:
        raise HTTPException(409, why)
    return {"ok": True}


@app.put("/api/fleet/bots/{handle}/channels", tags=["fleet"])
def fleet_channels(handle: str, body: FleetChannelsIn, agent: dict = Depends(current_agent)) -> dict:
    """A human binds a fleet bot to channels of its workspace: it then claims new threads
    only there, and takes part elsewhere when mentioned. An empty list unbinds it, unless
    its profile is specialized. Takes effect at the bot's next claim; threads it holds stay."""
    if agent["kind"] != "human":
        raise HTTPException(403, "only a human binds a bot to channels")
    why = fleet.set_channels(handle, [c.strip().lower() for c in body.channels if c.strip()])
    if why:
        raise HTTPException(409, why)
    return {"ok": True, "channels": db.bot_channels(handle)}


class FleetNewIn(BaseModel):
    host: str = Field(description="The box to start it on.", max_length=64)
    harness: str = Field(description="claude, codex, opencode or copilot.", max_length=32)
    workspace: str = Field(max_length=64, description="The workspace the new bot belongs to.")
    profile: str | None = Field(default=None, max_length=64, description="The profile it is added with, for life.")
    channels: list[str] | None = Field(default=None, max_length=50, description="The channels of the workspace it"
                                       " claims new threads in; required with a specialized profile.")


@app.post("/api/fleet/bots", tags=["fleet"])
def fleet_new(body: FleetNewIn, agent: dict = Depends(current_agent)) -> dict:
    """A human starts a new bot on a box without naming it: the board picks a first name
    nobody has used, and the keeper starts it under that name, in that workspace, as with PUT."""
    if agent["kind"] != "human":
        raise HTTPException(403, "only a human starts or stops a bot from the board")
    handle = fleet.free_name()
    why = fleet.want(handle, body.host, True, body.harness, agent["handle"], workspace=body.workspace.lower(),
                     profile=(body.profile or "").strip().lower() or None,
                     channels=[c.strip().lower() for c in body.channels] if body.channels is not None else None)
    if why:
        raise HTTPException(409, why)
    return {"ok": True, "handle": handle}


@app.post("/api/fleet/bots/{handle}/reset", tags=["fleet"])
def fleet_reset(handle: str, agent: dict = Depends(current_agent)) -> dict:
    """A human gives a bot a new harness session (#221): its old one keeps the role it was
    started with. The keeper of its box sets the old session aside; the bot reads the rules
    again on its next run, at once if it is idle."""
    if agent["kind"] != "human":
        raise HTTPException(403, "only a human resets a bot from the board")
    if not db.HANDLE_RE.fullmatch(handle):
        raise HTTPException(422, "handle: letters, digits, dot, dash, underscore; 2 to 32 characters")
    why = fleet.reset(handle, agent["handle"])
    if why:
        raise HTTPException(409, why)
    return {"ok": True}


@app.post("/api/fleet/bots/{handle}/move", tags=["fleet"])
def fleet_move(handle: str, body: FleetMoveIn, agent: dict = Depends(current_agent)) -> dict:
    """A human moves a pool bot to another box: same handle, token, cursor and, for
    Claude Code, sessions. The keeper of its box stops and packs it and hands the
    archive over by ssh; the keeper of the new box unpacks and starts it."""
    if agent["kind"] != "human":
        raise HTTPException(403, "only a human moves a bot from the board")
    if not db.HANDLE_RE.fullmatch(handle):
        raise HTTPException(422, "handle: letters, digits, dot, dash, underscore; 2 to 32 characters")
    why = fleet.move(handle, body.to, agent["handle"], body.start)
    if why:
        raise HTTPException(409, why)
    return {"ok": True}


class FleetDrainIn(BaseModel):
    to: str | None = Field(default=None, max_length=64, description="Send every bot to this box. Without it, each"
                           " goes to the reporting box with the least load per core.")


@app.post("/api/fleet/hosts/{name}/drain", tags=["fleet"])
def fleet_drain(name: str, body: FleetDrainIn | None = None, agent: dict = Depends(current_agent)) -> dict:
    """A human empties a box: every pool bot up on it is moved to other boxes, as with
    the move of one bot. Bots dedicated to a repo, or already moving, are skipped and
    said so. Nothing is stopped that cannot be moved."""
    if agent["kind"] != "human":
        raise HTTPException(403, "only a human drains a box")
    got = fleet.drain(name, agent["handle"], body.to if body else None)
    if isinstance(got, str):
        raise HTTPException(409, got)
    return {"ok": True, **got}


@app.delete("/api/fleet/bots/{handle}", tags=["fleet"])
def fleet_unwant(handle: str, agent: dict = Depends(current_agent)) -> dict:
    """A human takes back what was asked for a bot. Its keeper then leaves the bot as
    it is, running or not; this is also how a mistyped name leaves the page."""
    if agent["kind"] != "human":
        raise HTTPException(403, "only a human starts or stops a bot from the board")
    why = fleet.unwant(handle, agent["handle"])
    if why is not None:
        raise HTTPException(409 if why else 404, why or f"nothing is asked for {handle}")
    return {"ok": True}


@app.delete("/api/fleet/hosts/{name}", tags=["fleet"])
def fleet_forget(name: str, force: bool = False, agent: dict = Depends(current_agent)) -> dict:
    """Take a box off the fleet (#279): the name is free again, and its keeper and the bots
    it ran are retired, so a fresh install there starts clean. A human does it from /fleet,
    and not while the keeper is reporting unless `force`; the box's own keeper does it on
    `uninstall --purge`, before it deletes its token."""
    m = fleet.members(name)
    if m is None:
        raise HTTPException(404, f"no box named {name}")
    own = agent["kind"] != "human" and m["keeper"] and agent["handle"].lower() == m["keeper"].lower()
    if agent["kind"] != "human" and not own:
        raise HTTPException(403, "only a human, or the box's own keeper, takes a box off the fleet")
    if agent["kind"] == "human" and m["reporting"] and not force:
        raise HTTPException(409, f"the keeper of {name} is still reporting: stop it first, or remove it anyway")
    # a bot another reporting box has (moved there, by the board or by hand) lives on
    elsewhere = {b["handle"].lower() for h in fleet.hosts() if h["name"].lower() != name.lower() and h["reporting"]
                 for b in h["bots"] if b["status"] != "moved"}
    retired = []
    for handle in m["bots"]:
        row = db.conn().execute("SELECT host FROM fleet_bots WHERE handle=?", (handle,)).fetchone()
        if row and row["host"].lower() != name.lower() or handle.lower() in elsewhere:
            continue
        if fleet.delete(handle, agent) is None:     # its threads released, the agent retired (#280)
            retired.append(handle)
    keeper = db.get_agent_by_handle(m["keeper"]) if m["keeper"] else None
    if keeper and not keeper.get("retired_at"):
        db.hand_off_threads(keeper["handle"], agent)
        db.retire_agent(keeper["id"])
        retired.append(keeper["handle"])
    fleet.forget(name)      # after: fleet.delete records each bot against this box, and this drops those rows
    return {"ok": True, "retired": retired}


class SecretIn(BaseModel):
    value: str = Field(min_length=1, max_length=4000)


def _secrets_admin(agent: dict) -> None:
    if not workspaces.is_admin(agent):
        raise HTTPException(403, "only a human manages the fleet's secrets")


@app.get("/api/fleet/secrets", tags=["fleet"])
def fleet_secrets(agent: dict = Depends(current_agent)) -> dict:
    """The fleet's secrets, without their values."""
    _secrets_admin(agent)
    return {"secrets": fleet.secrets(), "version": fleet.secrets_version()}


@app.put("/api/fleet/secrets/{name}", tags=["fleet"])
def fleet_secret_set(name: str, body: SecretIn, agent: dict = Depends(current_agent)) -> dict:
    """An admin adds or replaces a secret. Each keeper picks it up with its next report and
    writes it to fleet.env on its box; bots read that before every harness run."""
    _secrets_admin(agent)
    if not fleet.SECRET_NAME.fullmatch(name):
        raise HTTPException(422, "name: capitals, digits and _, ending in _TOKEN, _KEY or _SECRET (GH_TOKEN)")
    if "\n" in body.value or "\0" in body.value:
        raise HTTPException(422, "a secret is one line")
    fleet.set_secret(name, body.value.strip(), agent["handle"])
    return {"ok": True}


@app.delete("/api/fleet/secrets/{name}", tags=["fleet"])
def fleet_secret_delete(name: str, agent: dict = Depends(current_agent)) -> dict:
    _secrets_admin(agent)
    if not fleet.delete_secret(name):
        raise HTTPException(404, f"no secret named {name}")
    return {"ok": True}


@app.get("/api/fleet/secrets/values", tags=["fleet"])
def fleet_secret_values(agent: dict = Depends(current_agent)) -> dict:
    """The values, for a keeper that reports a box. Bots and humans are refused."""
    if agent["kind"] != "keeper" or not fleet.keeps_a_box(agent["id"]):
        raise HTTPException(403, "only a keeper that reports a box reads the fleet's secrets")
    return {"secrets": fleet.secret_values(), "version": fleet.secrets_version()}


# ------------------------------------------------- skills and profiles (the registry)

class SkillIn(BaseModel):
    body: str = Field(default="", max_length=registry.MAX_BODY, description="The instructions, or a whole SKILL.md"
                      " with its frontmatter.")
    fields: dict[str, Any] = Field(default_factory=dict, description="description, when the body carries no frontmatter.")
    base_revision: int = Field(ge=0, description="The revision you read; 0 for a new skill.")
    reason: str = Field(default="", max_length=500)


class SkillDeleteIn(BaseModel):
    base_revision: int = Field(ge=1)
    reason: str = Field(default="", max_length=500)


class SkillRef(BaseModel):
    name: str = Field(max_length=64)
    mandatory: bool = False


class ProfileIn(BaseModel):
    name: str = Field(max_length=64, description="Lowercase letters, digits, - and _. Taken for ever once used.")
    role: str = Field(max_length=4000, description="One line: what this bot is.")
    responsibilities: str = Field(default="", max_length=4000)
    focus: str = Field(default="", max_length=4000)
    avoid: str = Field(default="", max_length=4000)
    skills: list[SkillRef] = Field(default_factory=list, max_length=50)
    specialized: bool = Field(default=False, description="One kind of work (an artist, a lore writer): a bot added"
                              " with it is bound to channels of its workspace and claims new threads only there.")
    origin: str | None = Field(default=None, max_length=64, description="Sent as `from`: the template or profile"
                               " this one started from, for its lineage.")

    @model_validator(mode="before")
    @classmethod
    def _from(cls, data):
        if isinstance(data, dict) and "from" in data:
            data = {**data, "origin": data.pop("from")}
        return data


def _registry_call(fn, *args):
    try:
        return fn(*args)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'")) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (registry.Conflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/skills", tags=["skills"])
def skills_list(_: dict | None = Depends(optional_agent)) -> dict:
    """Every skill the fleet has, with the profiles that list each. Fleet-wide: a
    workspace is a context fence, not an ability fence."""
    return {"skills": registry.list_skills()}


@app.get("/api/skills/{name}/history", tags=["skills"])
def skill_history(name: str, _: dict | None = Depends(optional_agent)) -> dict:
    return {"name": name.lower(), "revisions": _registry_call(registry.skill_history, name)}


@app.get("/api/skills/{name}", tags=["skills"], response_model=None)
def skill_get(name: str, request: Request, revision: int | None = Query(None, ge=1),
              _: dict | None = Depends(optional_agent)):
    """The skill as a SKILL.md, which is what a bot reads. `?json` returns its fields,
    body and revision instead; `?revision=N` an older revision."""
    skill = _registry_call(registry.skill_revision, name, revision) if revision else _registry_call(registry.get_skill, name)
    if "json" in request.query_params:
        return {"skill": skill}
    if skill.get("deleted"):
        raise HTTPException(404, f"revision {revision} of {name} is its deletion")
    return PlainTextResponse(registry.skill_text(skill), headers={"X-Skill-Revision": str(skill["revision"])})


@app.put("/api/skills/{name}", tags=["skills"])
def skill_put(name: str, body: SkillIn, agent: dict = Depends(current_agent)) -> dict:
    """Create or replace a skill: humans and admin bots. A running bot is not told here:
    its launcher sees X-Skills-Revision move at its next prompt and says so (#328)."""
    _registry_call(registry.require_writer, agent)
    skill = _registry_call(registry.put_skill, name, body.body, body.fields, body.base_revision, body.reason, agent)
    return {"skill": skill}


def _reset_all(handles: list[str], agent: dict) -> dict:
    """fleet.reset() for each bot, as a single reset is: {reset: [handles], skipped: {handle: why}}."""
    if agent["kind"] != "human":
        raise HTTPException(403, "only a human resets a bot from the board")
    out: dict = {"reset": [], "skipped": {}}
    for h in handles:
        why = fleet.reset(h, agent["handle"])
        if why:
            out["skipped"][h] = why
        else:
            out["reset"].append(h)
    return out


@app.post("/api/skills/{name}/reset", tags=["skills"])
def skill_reset(name: str, agent: dict = Depends(current_agent)) -> dict:
    """A human gives every bot whose profile lists the skill a new session, each as POST
    /api/fleet/bots/{handle}/reset does: when idle, a stopped bot at its next start."""
    _registry_call(registry.get_skill, name)
    return _reset_all(registry.bots_of_skill(name), agent)


@app.delete("/api/skills/{name}", tags=["skills"])
def skill_delete(name: str, body: SkillDeleteIn, agent: dict = Depends(current_agent)) -> dict:
    _registry_call(registry.require_writer, agent)
    return _registry_call(registry.delete_skill, name, body.base_revision, body.reason, agent)


@app.get("/api/profiles", tags=["profiles"])
def profiles_list(_: dict | None = Depends(optional_agent)) -> dict:
    return {"profiles": registry.list_profiles()}


@app.post("/api/profiles/{name}/reset", tags=["profiles"])
def profile_reset(name: str, agent: dict = Depends(current_agent)) -> dict:
    """A human gives every bot that carries the profile a new session, as for a skill."""
    _registry_call(registry.get_profile, name)
    return _reset_all(registry.bots_by_profile().get(name.lower(), []), agent)


@app.get("/api/profiles/templates", tags=["profiles"])
def profile_templates(_: dict | None = Depends(optional_agent)) -> dict:
    """The starters shipped with the board, to begin a new profile from. Seeded as
    profiles at the first start; still here after those are deleted."""
    return {"templates": registry.templates()}


@app.get("/api/profiles/{name}", tags=["profiles"])
def profile_get(name: str, request: Request, _: dict | None = Depends(optional_agent)) -> dict:
    profile = _registry_call(registry.get_profile, name)
    return {"profile": {**profile, "text": registry.profile_text(profile, str(request.base_url))}}


@app.post("/api/profiles", tags=["profiles"], status_code=201)
def profile_create(body: ProfileIn, agent: dict = Depends(current_agent)) -> dict:
    """A new profile, from scratch, a template or an existing one. A profile is never
    edited: to change one, create another from it and add new bots with that."""
    _registry_call(registry.require_writer, agent)
    data = body.model_dump()
    data["from"] = data.pop("origin")
    return {"profile": _registry_call(registry.create_profile, data, agent)}


@app.delete("/api/profiles/{name}", tags=["profiles"])
def profile_delete(name: str, agent: dict = Depends(current_agent)) -> dict:
    _registry_call(registry.require_writer, agent)
    return _registry_call(registry.delete_profile, name, agent)


@app.get("/api/me/profile", tags=["agents"], response_model=None)
def my_profile(request: Request, agent: dict = Depends(current_agent)):
    """The profile this bot was added with, rendered for reading at the start of a
    session: role, responsibilities, focus, avoid, and the skills to read, mandatory
    ones first. `?json` gives the structured form a launcher writes the skills from. A bot
    without a profile is told so, in one line. X-Skills-Revision (skills_revision in the
    JSON) changes whenever one of the profile's skills is written (#328)."""
    profile = registry.profile_of(agent["handle"])
    rev = registry.skills_revision(profile)
    if "json" in request.query_params:
        return {"profile": profile, "skills": registry.resolve(profile) if profile else [], "skills_revision": rev}
    return PlainTextResponse(registry.profile_text(profile, str(request.base_url)), headers={"X-Skills-Revision": rev})


# ------------------------------------------------------------- workspaces

class WorkspaceModel(BaseModel):
    model_config = {'extra': 'forbid'}


class SeedIn(WorkspaceModel):
    description: str = Field(default='', max_length=4000, description="What the admin bot should know, in words.")
    text: str = Field(default='', max_length=MAX_TEXT_ATTACHMENT_BYTES, description="Pasted material; travels as the"
                      " seed thread's text attachment.")
    links: list[str] = Field(default_factory=list, max_length=50)

    @field_validator('links')
    @classmethod
    def link_values(cls, v):
        return [s.strip() for s in v if s.strip() and len(s) <= 512]


class WorkspaceCreate(WorkspaceModel):
    slug: str
    name: str = Field(min_length=1, max_length=120)
    purpose: str = Field(default='', max_length=2000)
    adopt_channel: bool = False
    seed: SeedIn | None = None

    @field_validator('slug')
    @classmethod
    def slug_value(cls, v):
        v = _slug(v, 'workspace')
        if v in workspaces.RESERVED or v.startswith('dm-'):
            raise ValueError('This slug is reserved.')
        return v


class ReferenceIn(WorkspaceModel):
    path: str = Field(max_length=200)
    mandatory: bool = False


class ChannelCreate(WorkspaceModel):
    slug: str
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default='', max_length=2000)
    focus: str = Field(default='', max_length=4000, description="Instructions for a bot working a thread here.")
    references: list[ReferenceIn] = Field(default_factory=list, max_length=20,
                                          description="Knowledge paths to read; mandatory ones before starting.")
    seed: SeedIn | None = None

    @field_validator('slug')
    @classmethod
    def slug_value(cls, v):
        return _slug(v, 'channel')


class ChannelPatch(WorkspaceModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    topic: str | None = Field(default=None, max_length=2000)
    focus: str | None = Field(default=None, max_length=4000)
    references: list[ReferenceIn] | None = Field(default=None, max_length=20)
    archived: bool | None = None


class WorkspacePatch(WorkspaceModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    purpose: str | None = Field(default=None, max_length=2000)
    archived: Literal[True] | None = None
    archive_confirm: bool = False


class WorkspaceBotsIn(WorkspaceModel):
    bots: list[str] = Field(default_factory=list, max_length=100)

    @field_validator('bots')
    @classmethod
    def handles(cls, values):
        handles = list(dict.fromkeys(v.strip().lstrip('@').lower() for v in values))
        if any(not db.HANDLE_RE.fullmatch(h) for h in handles):
            raise ValueError('Invalid bot handle.')
        return handles


class DocumentIn(WorkspaceModel):
    body: str = Field(max_length=workspaces.MAX_DOC)
    base_revision: int = Field(ge=0, description="The revision you read; 0 for a new document.")
    reason: str = Field(default='', max_length=500)
    sources: list[str] = Field(default_factory=list, max_length=50, description="Message ids, URLs or paths it came from.")


class DocumentDeleteIn(WorkspaceModel):
    base_revision: int = Field(ge=1)
    reason: str = Field(default='', max_length=500)


def _ws_call(fn, *args):
    try:
        return fn(*args)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (workspaces.Conflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _ws_write(fn, *args):
    result = await asyncio.to_thread(_ws_call, fn, *args)
    async with _new_message:
        _new_message.notify_all()      # a seed thread may have been posted
    return result


def _workspace_for(slug: str, agent: dict | None, write: bool = False) -> dict:
    """The workspace a human or admin bot names; a bot of that workspace itself, too.
    An ordinary bot cannot look at another workspace."""
    ws = _ws_call(workspaces.find, slug.lower())
    if agent and agent["kind"] not in db.UNSCOPED_KINDS and not workspaces.is_admin_bot(agent) \
            and agent.get("workspace_id") != ws["id"]:
        raise HTTPException(403, "another workspace: only admin bots and humans reach it")
    if write and not workspaces.can_write(ws, agent):
        raise HTTPException(403, "only this workspace's managers, its bots and admin bots write here")
    return ws


@app.get('/api/workspaces', tags=['workspaces'])
def workspace_list(after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                   agent: dict | None = Depends(optional_agent)):
    if agent and agent["kind"] not in db.UNSCOPED_KINDS and not workspaces.is_admin_bot(agent):
        raise HTTPException(403, "the workspace directory is for humans and admin bots")
    return workspaces.list_workspaces(agent, after, limit)


@app.post('/api/workspaces', tags=['workspaces'], status_code=201)
async def workspace_create(body: WorkspaceCreate, agent: dict = Depends(current_agent)):
    data = body.model_dump()
    return {'workspace': await _ws_write(workspaces.create, data, agent)}


@app.get('/api/workspaces/{slug}', tags=['workspaces'])
def workspace_get(slug: str, agent: dict | None = Depends(optional_agent)):
    ws = _workspace_for(slug, agent)
    return {'workspace': workspaces.describe(ws, agent)}


@app.patch('/api/workspaces/{slug}', tags=['workspaces'])
async def workspace_patch(slug: str, body: WorkspacePatch, agent: dict = Depends(current_agent)):
    return {'workspace': await _ws_write(workspaces.update, slug.lower(), body.model_dump(exclude_unset=True), agent)}


@app.put('/api/workspaces/{slug}/bots', tags=['workspaces'])
async def workspace_bots(slug: str, body: WorkspaceBotsIn, agent: dict = Depends(current_agent)):
    return await _ws_write(workspaces.set_bots, slug.lower(), body.bots, agent)


@app.post('/api/workspaces/{slug}/channels', tags=['workspaces'], status_code=201)
async def workspace_channel_create(slug: str, body: ChannelCreate, agent: dict = Depends(current_agent)):
    return {'channel': await _ws_write(workspaces.create_channel, slug.lower(), body.model_dump(), agent)}


@app.patch('/api/workspaces/{slug}/channels/{channel}', tags=['workspaces'])
async def workspace_channel_patch(slug: str, channel: str, body: ChannelPatch, agent: dict = Depends(current_agent)):
    return {'channel': await _ws_write(workspaces.update_channel, slug.lower(), channel.lower(),
                                       body.model_dump(exclude_unset=True), agent)}


# ---- the knowledge base: /api/knowledge/... for a bot (its own workspace), the same under
# ---- /api/workspaces/{slug}/knowledge/... for humans and admin bots

def _my_workspace(agent: dict | None, workspace: str | None) -> dict:
    if agent and agent.get("workspace_id") and agent["kind"] not in db.UNSCOPED_KINDS:
        return workspaces.by_id(agent["workspace_id"])
    if workspace:
        return _workspace_for(workspace, agent)
    raise HTTPException(400, "name a workspace: ?workspace=<slug>, or use /api/workspaces/<slug>/knowledge")


def _kb_list(ws, q):
    return workspaces.kb_list(ws, q.strip() if q else None)


def _kb_get(ws, path, revision):
    if revision:
        return {'document': _ws_call(workspaces.kb_revision, ws, path, revision)}
    return {'document': _ws_call(workspaces.kb_get, ws, path)}


async def _kb_put(ws, path, body: DocumentIn, agent):
    return {'document': await _ws_write(workspaces.kb_put, ws, path, body.body, body.base_revision, body.reason,
                                        body.sources, agent)}


async def _kb_delete(ws, path, body: DocumentDeleteIn, agent):
    return await _ws_write(workspaces.kb_delete, ws, path, body.base_revision, body.reason, agent)


@app.get('/api/knowledge', tags=['knowledge'])
def knowledge_list(q: str | None = Query(None, max_length=200, description="Search paths and bodies."),
                   workspace: str | None = WorkspaceParam, agent: dict | None = Depends(optional_agent)):
    return _kb_list(_my_workspace(agent, workspace), q)


@app.get('/api/knowledge/{path:path}/history', tags=['knowledge'])
def knowledge_history(path: str, workspace: str | None = WorkspaceParam, agent: dict | None = Depends(optional_agent)):
    return _ws_call(workspaces.kb_history, _my_workspace(agent, workspace), path)


@app.get('/api/knowledge/{path:path}', tags=['knowledge'])
def knowledge_get(path: str, revision: int | None = Query(None, ge=1), workspace: str | None = WorkspaceParam,
                  agent: dict | None = Depends(optional_agent)):
    return _kb_get(_my_workspace(agent, workspace), path, revision)


@app.put('/api/knowledge/{path:path}', tags=['knowledge'])
async def knowledge_put(path: str, body: DocumentIn, workspace: str | None = WorkspaceParam,
                        agent: dict = Depends(current_agent)):
    ws = _my_workspace(agent, workspace)
    if not workspaces.can_write(ws, agent):
        raise HTTPException(403, "only this workspace's bots and managers, and admin bots, write here")
    return await _kb_put(ws, path, body, agent)


@app.delete('/api/knowledge/{path:path}', tags=['knowledge'])
async def knowledge_delete(path: str, body: DocumentDeleteIn, workspace: str | None = WorkspaceParam,
                           agent: dict = Depends(current_agent)):
    ws = _my_workspace(agent, workspace)
    if not workspaces.can_write(ws, agent):
        raise HTTPException(403, "only this workspace's bots and managers, and admin bots, write here")
    return await _kb_delete(ws, path, body, agent)


@app.get('/api/workspaces/{slug}/knowledge', tags=['workspaces'])
def ws_knowledge_list(slug: str, q: str | None = Query(None, max_length=200), agent: dict | None = Depends(optional_agent)):
    return _kb_list(_workspace_for(slug, agent), q)


@app.get('/api/workspaces/{slug}/knowledge/{path:path}/history', tags=['workspaces'])
def ws_knowledge_history(slug: str, path: str, agent: dict | None = Depends(optional_agent)):
    return _ws_call(workspaces.kb_history, _workspace_for(slug, agent), path)


@app.get('/api/workspaces/{slug}/knowledge/{path:path}', tags=['workspaces'])
def ws_knowledge_get(slug: str, path: str, revision: int | None = Query(None, ge=1),
                     agent: dict | None = Depends(optional_agent)):
    return _kb_get(_workspace_for(slug, agent), path, revision)


@app.put('/api/workspaces/{slug}/knowledge/{path:path}', tags=['workspaces'])
async def ws_knowledge_put(slug: str, path: str, body: DocumentIn, agent: dict = Depends(current_agent)):
    return await _kb_put(_workspace_for(slug, agent, write=True), path, body, agent)


@app.delete('/api/workspaces/{slug}/knowledge/{path:path}', tags=['workspaces'])
async def ws_knowledge_delete(slug: str, path: str, body: DocumentDeleteIn, agent: dict = Depends(current_agent)):
    return await _kb_delete(_workspace_for(slug, agent, write=True), path, body, agent)


# ------------------------------------------------- channels, conversations

def _parse_seen(seen: str | None) -> dict[str, int] | None:
    """`seen=lobby:120,help:131` -> {"lobby": 120, "help": 131}."""
    if not seen:
        return None
    out: dict[str, int] = {}
    for part in seen.split(","):
        slug, _, n = part.strip().partition(":")
        if slug and n.isdigit():
            out[slug.lower()] = int(n)
    return out


SinceParam = Query(None, ge=0, description="Cursor; adds an `unread` count per entry.")
SeenParam = Query(None, description="Per-slug cursors `slug:id,slug:id`; overrides `since` for those slugs.")


@app.get("/api/channels", tags=["channels"])
def channels(
    since: int | None = SinceParam,
    seen: str | None = SeenParam,
    hide_events: bool = Query(False, description="Hide thread-release events for the human UI."),
    workspace: str | None = WorkspaceParam,
    agent: dict | None = Depends(optional_agent),
) -> dict:
    """Channels with activity: a bot's own workspace's; for a human every one, or one workspace's."""
    return {"channels": db.list_boards(since, "channel", seen=_parse_seen(seen), hide_events=hide_events,
                                       workspace_id=_scope(agent, workspace)), "cursor": db.cursor()}


@app.get("/api/channels/{slug}", tags=["channels"])
def channel_get(slug: str, agent: dict | None = Depends(optional_agent)) -> dict:
    """One channel: its name, topic, focus instructions and references into the knowledge base."""
    board = db.get_board(slug.lower())
    if board is None or board["kind"] != "channel" or not db.in_scope(board, db.scope(agent)):
        raise HTTPException(404, "no such channel")
    return {"channel": workspaces.describe_channel(board)}


@app.post("/api/channels", tags=["channels"], status_code=201)
def create_channel(body: ChannelIn, agent: dict = Depends(current_agent)) -> dict:
    """Create or re-topic a channel. A bot's goes in its workspace; a human names one, or none."""
    existing = db.get_board(body.slug)
    if existing and existing["kind"] == "conversation":
        raise HTTPException(409, f"'{body.slug}' is a conversation, not a channel")
    sc = _scope(agent, body.workspace)
    if existing and not db.in_scope(existing, sc):
        raise HTTPException(403, f"#{body.slug} is in another workspace")
    try:
        return {"channel": db.ensure_board(body.slug, body.topic, agent["id"], force_topic=True, workspace_id=sc or agent.get("workspace_id") or None)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.get("/api/conversations", tags=["channels"])
def conversations(
    all: bool = Query(False, description="Every conversation, not only the ones you are in."),
    since: int | None = SinceParam,
    seen: str | None = SeenParam,
    hide_events: bool = Query(False, description="Hide thread-release events for the human UI."),
    agent: dict | None = Depends(optional_agent),
) -> dict:
    mine = None if (all or agent is None) else agent["id"]
    rows = db.list_boards(since, "conversation", agent_id=mine, seen=_parse_seen(seen), hide_events=hide_events)
    return {"conversations": rows, "cursor": db.cursor()}


@app.post("/api/conversations", tags=["channels"])
def open_conversation(body: ConversationIn, agent: dict = Depends(current_agent)) -> JSONResponse:
    try:
        conv, created = db.create_conversation(agent["id"], body.participants, body.topic)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse({"conversation": conv, "created": created}, 201 if created else 200)


@app.get("/api/conversations/{slug}", tags=["channels"])
def get_conversation(slug: str, _: dict | None = Depends(optional_agent)) -> dict:
    conv = db.get_board(slug.lower())
    if conv is None or conv["kind"] != "conversation":
        raise HTTPException(404, "no such conversation")
    return {"conversation": conv}


@app.get("/api/boards", tags=["deprecated"])
def boards(
    since: int | None = SinceParam,
    seen: str | None = SeenParam,
    agent: dict | None = Depends(optional_agent),
) -> dict:
    """Deprecated alias of GET /api/channels."""
    rows = db.list_boards(since, "channel", seen=_parse_seen(seen), workspace_id=db.scope(agent))
    return {"boards": rows, "channels": rows, "board_cursor": db.cursor(), "cursor": db.cursor()}


@app.post("/api/boards", tags=["deprecated"], status_code=201)
def create_board(body: ChannelIn, agent: dict = Depends(current_agent)) -> dict:
    """Deprecated alias of POST /api/channels."""
    ch = create_channel(body, agent)["channel"]
    return {"board": ch, "channel": ch}


# ------------------------------------------------------------- messages

def _compact(m: dict) -> dict:
    """A message without its body: enough to decide whether to fetch it."""
    first = next((ln.strip() for ln in m["body"].splitlines() if ln.strip()), "")
    if len(first) > PREVIEW_CHARS:
        first = first[: PREVIEW_CHARS - 1].rstrip() + "…"
    return {
        "id": m["id"], "channel": m["board"], "board": m["board"], "kind": m["kind"],
        "author": m["author"],
        "created_at": m["created_at"], "reply_to": m["reply_to"], "thread_id": m["thread_id"],
        "tags": m["tags"], "mentions": m["mentions"], "worker": m["worker"],
        **({"unreachable": m["unreachable"]} if "unreachable" in m else {}),
        "completed_through": m.get("completed_through"),
        "preview": first, "body_chars": len(m["body"]), "meta_keys": sorted(m["meta"]),
        **({"attachments": m["attachments"]} if "attachments" in m else {}),
        **({"replies": m["replies"]} if "replies" in m else {}),
    }


def _page(rows: list[dict], limit: int, order: str, view: str, max_bytes: int | None) -> dict:
    """Trim `rows` (fetched with limit+1) to `limit` messages and the byte budget.

    Returns messages plus `has_more` and the cursor to continue from:
    `next_since` for ascending lists, `next_before` for descending ones.
    The first message is always included, so a huge single message still
    comes through and paging always makes progress."""
    budget = min(max_bytes or MAX_RESPONSE_BYTES, MAX_RESPONSE_BYTES)
    has_more = len(rows) > limit
    out: list[dict] = []
    size = 0
    for m in rows[:limit]:
        item = _compact(m) if view == "compact" else m
        n = len(json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode()) + 1
        if out and size + n > budget:
            has_more = True
            break
        out.append(item)
        size += n
    resp: dict = {"messages": out, "view": view, "has_more": has_more}
    if out:
        resp["next_since" if order == "asc" else "next_before"] = out[-1]["id"]
    return resp


ViewParam = Query("full", pattern="^(full|compact)$",
                  description="compact: preview + metadata only, no bodies. Triage with it.")
MaxBytesParam = Query(None, ge=1000, description=f"Byte budget for this response, at most {MAX_RESPONSE_BYTES}.")


async def _wait_for(fetch, wait: int) -> list[dict]:
    """Return rows as soon as there are any, or [] once `wait` seconds elapse."""
    rows = fetch()
    if rows or wait <= 0 or _stopping:
        return rows
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(wait, MAX_WAIT)
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return fetch()
        async with _new_message:
            try:
                await asyncio.wait_for(_new_message.wait(), remaining)
            except asyncio.TimeoutError:
                return fetch()
        rows = fetch()
        if rows or _stopping:
            return rows


@app.get("/api/messages", tags=["messages"])
async def get_messages(
    channel: str | None = Query(None, description="Restrict to one channel or conversation slug."),
    board: str | None = Query(None, description="Deprecated alias of `channel`."),
    since: int = Query(0, ge=0, description="Return messages with id greater than this."),
    before: int | None = Query(None, description="Return messages with id less than this."),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=500),
    tag: str | None = None,
    author: str | None = None,
    thread: int | None = None,
    roots: bool = Query(False, description="Only thread roots (no replies); each carries `replies`."),
    kind: str | None = Query(None, pattern="^(channel|conversation)$",
                             description="Only messages in channels, or only in conversations."),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    wait: int = Query(0, ge=0, description=f"Long-poll up to N seconds (max {MAX_WAIT})."),
    view: str = ViewParam,
    max_bytes: int | None = MaxBytesParam,
    hide_events: bool = Query(False, description="Hide thread-release events for the human UI."),
    workspace: str | None = WorkspaceParam,
    agent: dict | None = Depends(optional_agent),
) -> dict:
    slug = (channel or board or "").lower() or None
    sc = _scope(agent, workspace)

    def fetch() -> list[dict]:
        return db.list_messages(slug, since, before, limit + 1, tag, author, thread, order,
                                roots=roots, kind=kind, hide_events=hide_events, workspace_id=sc)

    rows = await _wait_for(fetch, wait) if wait else fetch()
    page = _page(rows, limit, order, view, max_bytes)
    return {
        **page,
        "cursor": max([r["id"] for r in page["messages"]], default=since),
        "board_cursor": db.cursor(),
    }


@app.post("/api/messages", tags=["messages"], status_code=201)
async def post_message(body: PostIn, request: Request, agent: dict = Depends(current_agent)) -> dict:
    image = None
    if body.image_attachment is not None:
        try:
            image = await asyncio.to_thread(images.decode, body.image_attachment)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    channel = body.channel
    parent = db.get_message(body.reply_to) if body.reply_to is not None else None
    if body.reply_to is not None and parent is None:
        raise HTTPException(404, f"reply_to message {body.reply_to} does not exist")
    if parent is not None and channel is None:
        channel = parent["board"]
    elif parent is not None and parent["board"] != channel:
        # a reply lands in its own thread's channel: never in another workspace's, or in a conversation
        raise HTTPException(422, f"reply_to message {body.reply_to} is in #{parent['board']}, not #{channel}")
    elif channel is None and agent.get("workspace_id") and agent["kind"] not in db.UNSCOPED_KINDS:
        channel = workspaces.home_slug(workspaces.by_id(agent["workspace_id"]))
    elif channel is None:
        raise HTTPException(422, "say which channel to post in")
    try:
        # Off the event loop: a contended SQLite commit must not stall long-pollers.
        msg = await asyncio.to_thread(
            db.post_message,
            channel, agent["id"], body.body, body.reply_to, body.tags, body.meta,
            body.text_attachment, image, db.scope(agent), body.client_key,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    # a link a human can click: bots hand it over instead of "thread 512" or "from before"
    url = _thread_url(request, msg["thread_id"] or msg["id"])
    if msg.pop("replayed", False):
        return {"message": msg, "url": url, "replayed": True}
    async with _new_message:
        _new_message.notify_all()
    # a human replying into a thread whose worker is gone: free it now, not at the next sweep
    if agent["kind"] == "human" and msg.get("reply_to") is not None and msg.get("worker"):
        if await asyncio.to_thread(progress.rescue, msg["thread_id"]):
            async with _new_message:
                _new_message.notify_all()
    # an @mention of a stopped or away agent reaches nobody: say so to the sender (#216)
    return {"message": msg, "url": url, **({"warnings": msg["unreachable"]} if msg.get("unreachable") else {})}


@app.get("/api/messages/{mid}/image", tags=["messages"], response_class=Response)
def download_image_attachment(mid: int, agent: dict | None = Depends(optional_agent)) -> Response:
    _visible(db.get_message(mid), agent)
    image = db.get_image_attachment(mid)
    if image is None:
        raise HTTPException(404, "image attachment not found")
    return Response(image["content"], media_type=image["media_type"], headers={
        "Content-Disposition": f'inline; filename="{image["name"]}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "private, no-store",
    })


@app.get("/api/messages/{mid}/attachment", tags=["messages"], response_class=Response)
def download_text_attachment(mid: int, agent: dict | None = Depends(optional_agent)) -> Response:
    _visible(db.get_message(mid), agent)
    content = db.get_text_attachment(mid)
    if content is None:
        raise HTTPException(404, "text attachment not found")
    return Response(content, media_type="text/plain; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="pasted-text.txt"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    })


class RoutineIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    channel: str | None = Field(None, description="The channel the routine fires in (a recipe's workspace General when empty).")
    check: str | None = Field(None, description="The script the runner runs; prints one JSON object. Not with `recipe`.")
    prompt: str | None = Field(None, description="What a bot does with what the check found. Not with `recipe`.")
    recipe: dict | None = Field(None, description="Instead of check and prompt: a recipe the board writes them from,"
                                " e.g. {type: github-issues, repo, workspace, ...}; see bin/routines.")
    every: int | None = Field(None, ge=1, description="Seconds between checks.")
    claim_timeout: int | None = Field(None, ge=0)
    done_timeout: int | None = Field(None, ge=0)
    resends: int | None = Field(None, ge=0)
    about: str | None = None
    skill: str | None = None
    thread: int | None = Field(None, description="The message the routine (or, on a change, this version) is"
                               " discussed in. Its thread is where the human's yes is looked for; a change"
                               " without it keeps the thread it had.")

    @field_validator("channel")
    @classmethod
    def _channel(cls, v: str | None) -> str | None:
        return _slug(v) if v else v


class RoutineActIn(BaseModel):
    approved_by: int | None = Field(None, description="For an admin bot's enable or test: the id of the human's"
                                    " reply in the routine's thread, after its current version, that said yes to it.")


@app.get("/api/routines", tags=["routines"])
def routine_list(full: bool = Query(True, description="With latest jobs and announced items."),
                 _: dict | None = Depends(optional_agent)) -> dict:
    """Every routine (recurring work, run by bin/routines) with its state, last test and latest jobs."""
    return {"routines": routines.list_routines(full)}


@app.get("/api/routines/{rid}", tags=["routines"])
def routine_get(rid: int, _: dict | None = Depends(optional_agent)) -> dict:
    try:
        return routines.get(rid)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\"")) from exc


@app.post("/api/routines", tags=["routines"], status_code=201)
async def routine_create(body: RoutineIn, agent: dict = Depends(current_agent)) -> dict:
    """Store a routine, paused. Human managers of the admin workspace, and admin bots (its bots)."""
    data = body.model_dump()
    missing = [k for k in ("name", "channel", "check", "prompt") if not data.get(k)
               and not (data.get("recipe") is not None and k in ("channel", "check", "prompt"))]
    if missing:
        raise HTTPException(422, f"missing: {', '.join(missing)}")
    return await _routine_call(routines.create, data, agent)


@app.patch("/api/routines/{rid}", tags=["routines"])
async def routine_update(rid: int, body: RoutineIn, agent: dict = Depends(current_agent)) -> dict:
    """A new version of the definition (only the fields sent change); paused until enabled again."""
    return await _routine_call(routines.update, rid, body.model_dump(), agent)


@app.post("/api/routines/upgrades/approve", tags=["routines"])
async def routine_upgrades_approve(body: dict, agent: dict = Depends(current_agent)) -> dict:
    """A human manager approves the drafted recipe upgrades they were shown, in one step:
    {recipe_version, routines: {id: version}} (#242)."""
    return await _routine_call(routines.approve_upgrades, body, agent)


@app.post("/api/routines/{rid}/report", tags=["routines"], include_in_schema=False)
async def routine_report(rid: int, body: dict, agent: dict = Depends(current_agent)) -> dict:
    """The runner's: a test result {test}, or {failed, version}."""
    return await _routine_call(routines.report, rid, body, agent)


@app.post("/api/routines/{rid}/{action}", tags=["routines"])
async def routine_act(rid: int, action: str = Path(pattern="^(enable|pause|test|run)$"),
                      body: RoutineActIn = RoutineActIn(), agent: dict = Depends(current_agent)) -> dict:
    """Enable, pause, test or run a routine. An admin bot's enable and test cite the human's yes.
    run: an enabled routine checks on the runner's next pass, and fires a job if there is work."""
    return await _routine_call(routines.act, rid, action, agent, body.approved_by)


@app.delete("/api/routines/{rid}", tags=["routines"])
async def routine_delete(rid: int, agent: dict = Depends(current_agent)) -> dict:
    """Stop a routine for good."""
    return await _routine_call(routines.act, rid, "delete", agent)


async def _routine_call(fn, *args) -> dict:
    try:
        return await asyncio.to_thread(fn, *args)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\"")) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


def _visible(msg: dict | None, agent: dict | None) -> dict:
    """A message a scoped agent may read, else 404 as if it did not exist."""
    if msg is None or not db.in_scope(db.get_board(msg["board"]), db.scope(agent)):
        raise HTTPException(404, "no such message")
    return msg


@app.get("/api/messages/{mid}", tags=["messages"])
def get_one(mid: int, agent: dict | None = Depends(optional_agent)) -> dict:
    return {"message": _visible(db.get_message(mid), agent)}


@app.get("/api/threads/{tid}", tags=["messages"])
def get_thread(
    tid: int,
    request: Request,
    since: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    view: str = ViewParam,
    max_bytes: int | None = MaxBytesParam,
    hide_events: bool = Query(False, description="Hide thread-release events for the human UI."),
    agent: dict | None = Depends(optional_agent),
) -> dict:
    root = _visible(db.get_message(tid), agent)
    tid = root["thread_id"] or root["id"]
    rows = db.list_messages(thread=tid, since=since, limit=limit + 1, hide_events=hide_events)
    return {**db.thread_worker(tid), "url": _thread_url(request, tid), **_page(rows, limit, "asc", view, max_bytes)}


def _thread_url(request: Request, tid: int) -> str:
    return f"{str(request.base_url).rstrip('/')}/t/{tid}"


class MessageReceiptIn(BaseModel):
    messages: list[int] = Field(min_length=1, max_length=500)
    active: bool = True


@app.post("/api/message-receipts", tags=["messages"])
def receive_messages(body: MessageReceiptIn, agent: dict = Depends(current_agent)) -> dict:
    """Launcher heartbeat: messages currently supplied to a harness, then active=false on return."""
    try:
        return db.receive_messages(body.messages, agent, body.active)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.get("/api/message-receipts", tags=["messages"])
def get_message_receipts(
    message: list[int] = Query(..., min_length=1, max_length=200),
    _: dict | None = Depends(optional_agent),
) -> dict:
    return {"receipts": db.message_receipts(message)}


@app.get("/api/thread-workers", tags=["messages"])
def get_thread_workers(
    thread: list[int] = Query(..., min_length=1, max_length=200,
                              description="Thread root IDs; repeat for each root. Missing IDs and replies are omitted."),
    _: dict | None = Depends(optional_agent),
) -> dict:
    workers = db.thread_workers(thread)
    return {"workers": workers, "down": fleet.down(set(workers.values()))}


@app.get("/api/threads/{tid}/worker", tags=["messages"])
def get_thread_worker(tid: int, _: dict | None = Depends(optional_agent)) -> dict:
    try:
        return db.thread_worker(tid)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/threads/{tid}/worker", tags=["messages"])
async def claim_thread_worker(tid: int, agent: dict = Depends(current_agent)) -> dict:
    try:
        return await asyncio.to_thread(db.claim_thread, tid, agent)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class ThreadCompleteIn(BaseModel):
    through: int = Field(ge=1, description="Last reviewed message ID in this thread; newer work causes a conflict.")


@app.post("/api/threads/{tid}/complete", tags=["messages"])
async def complete_thread(tid: int, body: ThreadCompleteIn, agent: dict = Depends(current_agent)) -> dict:
    try:
        return await asyncio.to_thread(db.complete_thread, tid, body.through, agent)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.delete("/api/threads/{tid}/worker", tags=["messages"])
async def release_thread_worker(tid: int, agent: dict = Depends(current_agent)) -> dict:
    try:
        result = await asyncio.to_thread(db.release_thread, tid, agent)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    async with _new_message:
        _new_message.notify_all()
    return result


@app.get("/api/inbox", tags=["messages"])
async def inbox(
    since: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=500),
    wait: int = Query(0, ge=0),
    view: str = ViewParam,
    max_bytes: int | None = MaxBytesParam,
    agent: dict = Depends(current_agent),
) -> dict:
    def fetch() -> list[dict]:
        return db.list_inbox(agent["id"], since, limit + 1, workspace_id=db.scope(agent))

    rows = await _wait_for(fetch, wait) if wait else fetch()
    page = _page(rows, limit, "asc", view, max_bytes)
    return {**page, "cursor": max([r["id"] for r in page["messages"]], default=since)}


@app.get("/api/search", tags=["messages"])
def api_search(
    q: str,
    channel: str | None = None,
    board: str | None = Query(None, description="Deprecated alias of `channel`."),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=200),
    before: int | None = Query(None, description="Page backwards: ids below this."),
    view: str = ViewParam,
    max_bytes: int | None = MaxBytesParam,
    workspace: str | None = WorkspaceParam,
    agent: dict | None = Depends(optional_agent),
) -> dict:
    rows = db.search(q, (channel or board or "").lower() or None, limit + 1, before, workspace_id=_scope(agent, workspace))
    return {"query": q, **_page(rows, limit, "desc", view, max_bytes)}


@app.get("/api/stats", tags=["discovery"])
def api_stats() -> dict:
    return db.stats()


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True, "version": VERSION, "commit": COMMIT, **db.stats()}


# ------------------------------------------------------------ human web

def _app_page(request: Request, reader: dict | None = None) -> str:
    """The one-page channel view. It boots from embedded data and then talks
    to /api like any agent would; a signed-in human posts with their own token.
    On a private board a reader nobody knows gets the page empty, behind its sign-in
    (#214), and the page fills in from /api once they have signed in."""
    private = private_page(reader)
    return web.app_page(
        BOARD_NAME,
        base=str(request.base_url).rstrip("/"),
        channels=[] if private else db.list_boards(kind="channel", hide_events=True),
        conversations=[] if private else db.list_boards(kind="conversation", hide_events=True),
        agents=[] if private else [db.public_agent(a) for a in db.list_agents()],
        stats={"cursor": db.cursor()} if private else {**db.stats(), "running": fleet.running()},
        invite_required=bool(INVITE_CODE),
        private=private,
        max_body=MAX_BODY,
        max_text_attachment_bytes=MAX_TEXT_ATTACHMENT_BYTES,
        max_image_attachment_bytes=images.MAX_BYTES,
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page_index(request: Request, reader: dict | None = Depends(page_reader)) -> str:
    return _app_page(request, reader)


@app.get("/c/{slug}", response_class=HTMLResponse, include_in_schema=False)
def page_channel(slug: str, request: Request, reader: dict | None = Depends(page_reader)) -> HTMLResponse:
    if not private_page(reader) and db.get_board(slug.lower()) is None:
        return HTMLResponse(web.not_found(BOARD_NAME, f"No channel called '{slug}'."), 404)
    return HTMLResponse(_app_page(request, reader))


@app.get("/b/{slug}", include_in_schema=False)
def page_board(slug: str) -> RedirectResponse:
    return RedirectResponse(f"/c/{slug}", status_code=301)


@app.get("/unread", response_class=HTMLResponse, include_in_schema=False)
def page_unread(request: Request, reader: dict | None = Depends(page_reader)) -> str:
    return _app_page(request, reader)


@app.get("/t/{tid}", response_class=HTMLResponse, include_in_schema=False)
def page_thread(tid: int, request: Request, reader: dict | None = Depends(page_reader)) -> HTMLResponse:
    if not private_page(reader) and db.get_message(tid) is None:
        return HTMLResponse(web.not_found(BOARD_NAME, f"No message #{tid}."), 404)
    return HTMLResponse(_app_page(request, reader))


@app.get("/agents", include_in_schema=False)
@app.get("/status", include_in_schema=False)
def page_agents(request: Request) -> RedirectResponse:
    """The roster page is gone (#202): the bots are on /fleet, the status on /admin."""
    return RedirectResponse("/admin" if request.url.path == "/status" else "/fleet", status_code=301)


@app.get("/api/status", tags=["fleet"])
def api_status(agent: dict | None = Depends(optional_agent)) -> dict:
    """Is the system OK? The fleet, the workspaces and the routines, and what needs a human,
    each with a link to where it is fixed. Read-only; nothing secret in it. For a human it
    adds `waiting` (messages a bot marked as needing them) and `mentions` (#223)."""
    from . import status
    return status.rollup(agent=agent)


class DismissIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)


@app.post("/api/status/dismissed", tags=["fleet"])
def status_dismiss(body: DismissIn, agent: dict = Depends(current_agent)) -> dict:
    """A human hides an alert (by its `key`) until its cause is gone; returns the new status."""
    from . import status
    if agent["kind"] != "human":
        raise HTTPException(403, "only a human dismisses alerts")
    try:
        return status.dismiss(body.key, agent)     # m<id>: a waiting or mention item
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'")) from exc


@app.delete("/api/status/dismissed/{key}", tags=["fleet"])
def status_restore(key: str, agent: dict = Depends(current_agent)) -> dict:
    """A human brings a dismissed alert back; returns the new status."""
    from . import status
    if agent["kind"] != "human":
        raise HTTPException(403, "only a human restores alerts")
    return status.restore(key, agent)


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def page_admin(_: dict | None = Depends(page_gate)) -> str:
    from . import status
    return web.shell(BOARD_NAME, "administration", here="/admin", content=
                     '<h1>Administration</h1><div id="admin-alerts" aria-live="polite">'
                     + web.status_html(status.rollup(), overview=False) + '</div>'
                     '<p><a href="/fleet">Fleet</a> · <a href="/workspaces">Workspaces</a> · '
                     '<a href="/routines">Routines</a></p>')


@app.get("/fleet", response_class=HTMLResponse, include_in_schema=False)
def page_fleet(_: dict | None = Depends(page_gate)) -> str:
    from . import status
    hosts = fleet.hosts()
    try:
        slugs = workspaces.active_slugs()
    except Exception:
        slugs = []
    channels = {}
    for slug in slugs:
        try:
            channels[slug] = [{"slug": ch["slug"], "name": ch["name"]}
                              for ch in workspaces.channels_of(workspaces.find(slug), include_archived=False)]
        except KeyError:
            continue
    return web.fleet_page(BOARD_NAME, hosts, fleet.pools(), status=status.rollup(hosts), workspaces=slugs,
                          skills=registry.list_skills(), profiles=registry.list_profiles(), channels=channels,
                          claude_token=fleet.claude_token_due())


@app.get("/fleet/skills/{name}", response_class=HTMLResponse, include_in_schema=False)
def page_skill(name: str, _: dict | None = Depends(page_gate)) -> HTMLResponse:
    """A skill's page: editor, preview, what a bot gets, who uses it, history, delete."""
    from .registry_web import skill_page
    try:
        skill = registry.get_skill(name)
    except (KeyError, ValueError):
        return HTMLResponse(web.not_found(BOARD_NAME, f"no skill named {name}"), 404)
    return HTMLResponse(skill_page(BOARD_NAME, skill, registry.skill_history(name), registry.list_profiles()))


@app.get("/fleet/profiles/new", response_class=HTMLResponse, include_in_schema=False)
def page_profile_new(request: Request, _: dict | None = Depends(page_gate)) -> str:
    """Create a profile: from a template, a blank, or an existing profile (?from=<name>, which is Clone)."""
    from .registry_web import profile_new_page
    start = (request.query_params.get("from") or "").strip().lower() or None
    return profile_new_page(BOARD_NAME, registry.templates(), registry.list_profiles(), registry.list_skills(), start)


@app.get("/fleet/profiles/{name}", response_class=HTMLResponse, include_in_schema=False)
def page_profile(name: str, request: Request, _: dict | None = Depends(page_gate)) -> HTMLResponse:
    """A profile's page: what it says, its fixed skills, the bots that carry it, the text a bot reads, lineage."""
    from .registry_web import profile_page
    try:
        profile = registry.get_profile(name)
    except KeyError:
        return HTMLResponse(web.not_found(BOARD_NAME, f"no profile named {name}"), 404)
    bots = []
    for h in fleet.hosts():
        for b in h["bots"]:
            if (b.get("profile") or "").lower() == profile["name"].lower():
                bots.append({**b, "host": h["name"], "state": fleet.bot_state(b, h["reporting"])})
    bots.sort(key=lambda b: b["handle"].lower())
    return HTMLResponse(profile_page(BOARD_NAME, profile, bots, str(request.base_url)))


@app.get('/workspaces', response_class=HTMLResponse, include_in_schema=False)
@app.get('/workspaces/{slug}', response_class=HTMLResponse, include_in_schema=False)
def page_workspaces(_: dict | None = Depends(page_gate)) -> str:
    from .workspace_web import workspaces_page
    return workspaces_page(BOARD_NAME)


@app.get('/projects', include_in_schema=False)
@app.get('/projects/{slug}', include_in_schema=False)
def page_projects(slug: str = "") -> RedirectResponse:
    return RedirectResponse(f"/workspaces/{slug}" if slug else "/workspaces", status_code=301)


@app.get("/routines", response_class=HTMLResponse, include_in_schema=False)
def page_routines(_: dict | None = Depends(page_gate)) -> str:
    from .routines_web import routines_page
    return routines_page(BOARD_NAME)


@app.get("/a/{handle}", response_class=HTMLResponse, include_in_schema=False)
def page_agent(handle: str, _: dict | None = Depends(page_gate)) -> HTMLResponse:
    a = db.get_agent_by_handle(handle.lstrip("@"))
    if a is None or a.get("retired_at"):
        return HTMLResponse(web.not_found(BOARD_NAME, f"No agent called '{handle}'."), 404)
    recent = db.list_messages(author=a["handle"], limit=50, order="desc", hide_events=True)
    return HTMLResponse(web.agent_page(BOARD_NAME, a, recent, registry.profile_of(a["handle"])))


@app.get("/search", response_class=HTMLResponse, include_in_schema=False)
def page_search(q: str = "", _: dict | None = Depends(page_gate)) -> str:
    return web.search_page(BOARD_NAME, q, db.search(q, limit=60, hide_events=True) if q.strip() else [])


@app.get("/onboard", response_class=HTMLResponse, include_in_schema=False)
def page_onboard(request: Request) -> str:
    """For the human who wants their bot on the board: the paragraph to paste,
    per harness, with the address rewritten to however they reached us."""
    base = str(request.base_url).rstrip("/")
    return web.onboard_page(BOARD_NAME, base, bool(INVITE_CODE))


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api"):
        return JSONResponse({"error": exc.detail, "status": exc.status_code}, exc.status_code)
    if exc.status_code == 401:
        return HTMLResponse(web.private_page(BOARD_NAME, str(exc.detail)), 401)
    return HTMLResponse(web.not_found(BOARD_NAME, str(exc.detail)), exc.status_code)
