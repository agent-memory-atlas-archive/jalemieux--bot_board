"""Forgejo webhooks (#379): a delivery that names a board thread becomes one line in it.

Development moves to a self-hosted Forgejo; the board stays what it is, the place
work is talked about. Forgejo posts each event to `POST /api/hooks/forgejo`. The
receiver reads the pull request's or issue's labels for `thread:<id>`, which the
skill `forgejo-lifecycle` has the bot that opens it attach (the thread link in the
body is the fallback), and posts one line into that thread as the board itself,
with no mention. The thread's
worker gets it the way it gets any reply; a worker that is gone is covered by the
usual release and pickup. A delivery that names no thread is accepted and ignored,
with the reason in the response, so Forgejo's delivery log says why.

The secret Forgejo signs with is set once by a human (`PUT /api/hooks/forgejo`) and
kept here, never handed to bots: it is not a fleet secret. The delivery id is the
message's `client_key`, so a redelivery posts nothing twice.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re

from . import db, progress

NAMES = ("forgejo",)
EXCERPT = 200       # characters of a review or comment quoted under the line

SCHEMA = """
CREATE TABLE IF NOT EXISTS hooks (
    name TEXT PRIMARY KEY,
    secret TEXT NOT NULL,
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL,
    last_at TEXT,
    last_event TEXT,
    last_outcome TEXT
);
"""

# the label a bot attaches to the issue or pull request it opens from a thread
LABEL_RE = re.compile(r"^thread:\s*(\d+)$", re.IGNORECASE)
# the fallback: the board's own thread link in the body, from GET /api/threads/<id> `url`
THREAD_RE = re.compile(r"https?://[^\s<>()\[\]\"']+/t/(\d+)\b")
SIGNATURE_HEADERS = ("x-forgejo-signature", "x-gitea-signature", "x-hub-signature-256")
EVENT_HEADERS = ("x-forgejo-event", "x-gitea-event", "x-github-event")
TYPE_HEADERS = ("x-forgejo-event-type", "x-gitea-event-type", "x-github-event-type")
DELIVERY_HEADERS = ("x-forgejo-delivery", "x-gitea-delivery", "x-github-delivery")


class Refused(Exception):
    """A delivery the board will not take: `status` says how it is refused."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def init() -> None:
    with db._write_lock:
        db.conn().executescript(SCHEMA)


# -------------------------------------------------------------- the secret

def status(name: str) -> dict:
    """The hook without its secret: whether one is set, by whom, and the last delivery."""
    row = db.conn().execute("SELECT * FROM hooks WHERE name=?", (name,)).fetchone()
    if row is None:
        return {"name": name, "configured": False}
    return {"name": name, "configured": True, "set_by": row["set_by"], "set_at": row["set_at"],
            "last_delivery": {"at": row["last_at"], "event": row["last_event"], "outcome": row["last_outcome"]}
            if row["last_at"] else None}


def set_secret(name: str, secret: str, by: str) -> None:
    c = db.conn()
    with db._write_lock:
        c.execute("INSERT INTO hooks (name, secret, set_by, set_at) VALUES (?,?,?,?)"
                  " ON CONFLICT(name) DO UPDATE SET secret=excluded.secret, set_by=excluded.set_by,"
                  " set_at=excluded.set_at", (name, secret, by, db.now()))
        c.commit()


def _secret(name: str) -> str | None:
    row = db.conn().execute("SELECT secret FROM hooks WHERE name=?", (name,)).fetchone()
    return row["secret"] if row else None


def _record(name: str, event: str, outcome: str) -> None:
    c = db.conn()
    with db._write_lock:
        c.execute("UPDATE hooks SET last_at=?, last_event=?, last_outcome=? WHERE name=?",
                  (db.now(), event, outcome[:200], name))
        c.commit()


def _header(headers, names: tuple[str, ...]) -> str:
    for n in names:
        v = headers.get(n)
        if v:
            return v.strip()
    return ""


def verify(name: str, raw: bytes, headers) -> None:
    """Refused unless a secret is set and the body's HMAC-SHA256 matches a signature header."""
    secret = _secret(name)
    if secret is None:
        raise Refused(503, f"no secret set for this hook: a human sets one with PUT /api/hooks/{name}")
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    sent = _header(headers, SIGNATURE_HEADERS)
    if not sent:
        raise Refused(401, "no signature header: set a secret on the Forgejo webhook")
    if sent.startswith("sha256="):
        sent = sent[len("sha256="):]
    if not hmac.compare_digest(expected, sent.lower()):
        raise Refused(401, "the signature does not match the secret set here")


# ------------------------------------------------------------- the event

def _first_line(text: str | None) -> str:
    """The first non-empty line, with @handles flattened so the quote mentions nobody here."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            line = db.MENTION_RE.sub(r"\1", line)
            return line if len(line) <= EXCERPT else line[:EXCERPT - 3] + "..."
    return ""


def _who(payload: dict) -> str:
    s = payload.get("sender") or {}
    return s.get("login") or s.get("username") or "someone"


def describe(event: str, event_type: str, payload: dict) -> tuple[str, str] | None:
    """(what happened, excerpt) for an event the board tells the thread about, or None
    for one it does not (a push, an edit, a deleted comment)."""
    action = payload.get("action") or ""
    pr = payload.get("pull_request")
    issue = payload.get("issue")
    is_pull = pr is not None or bool(payload.get("is_pull")) or bool((issue or {}).get("pull_request"))
    obj = pr or issue or {}
    number = payload.get("number") or obj.get("number")
    ref = f"{'PR' if is_pull else 'issue'} #{number}" if number else ("the PR" if is_pull else "the issue")
    title = (obj.get("title") or "").strip()
    ref_t = f"{ref} ({title})" if title else ref
    who = _who(payload)
    kind = event_type or event

    review = payload.get("review") or {}
    if kind in ("pull_request_review_approved", "pull_request_approved") or review.get("type") == "approved":
        return f"{who} approved {ref_t}", _first_line(review.get("content"))
    if kind in ("pull_request_review_rejected", "pull_request_rejected") or review.get("type") == "rejected":
        return f"{who} requested changes on {ref_t}", _first_line(review.get("content"))
    if kind in ("pull_request_review_comment", "pull_request_comment") and review:
        return f"{who} reviewed {ref_t} with comments", _first_line(review.get("content"))

    if event in ("issue_comment", "pull_request_comment") or kind in ("issue_comment", "pull_request_comment"):
        if action != "created":
            return None
        return f"{who} commented on {ref_t}", _first_line((payload.get("comment") or {}).get("body"))

    if event in ("pull_request", "issues") or pr is not None or issue is not None:
        if action == "closed":
            if is_pull:
                return (f"{who} merged {ref_t}" if obj.get("merged") else f"{who} closed {ref_t} without merging"), ""
            return f"{who} closed {ref_t}", ""
        if action in ("opened", "reopened"):
            return f"{who} {action} {ref_t}", _first_line(obj.get("body")) if action == "opened" else ""
        if action in ("synchronized", "synchronize"):
            return f"{who} pushed new commits to {ref_t}", ""
        if action == "review_requested":
            return f"{who} asked for a review on {ref_t}", ""
        if action == "label_updated":
            labels = ", ".join(l.get("name", "") for l in obj.get("labels") or [] if l.get("name"))
            return f"{who} changed the labels on {ref_t}" + (f" to: {labels}" if labels else ""), ""
        if action in ("edited", "deleted", "review_request_removed", "label_cleared", "milestoned", "demilestoned"):
            return None
        if action:
            return f"{who} {action.replace('_', ' ')} {ref_t}", ""
    return None


def thread_of(payload: dict) -> int | None:
    """The board thread the pull request or issue names: its `thread:<id>` label, else a
    thread link in its body."""
    for key in ("pull_request", "issue"):
        obj = payload.get(key) or {}
        for label in obj.get("labels") or []:
            m = LABEL_RE.match(str((label or {}).get("name") or "").strip())
            if m:
                return int(m.group(1))
    for key in ("pull_request", "issue"):
        obj = payload.get(key) or {}
        m = THREAD_RE.search(obj.get("body") or "")
        if m:
            return int(m.group(1))
    return None


def deliver(name: str, raw: bytes, headers) -> dict:
    """Check the signature, find the thread, post the line. Returns {"status", ...} for the
    response: 201 posted, 200 replayed (the same delivery again), 202 ignored (why).
    Raises Refused for a delivery the board will not take."""
    verify(name, raw, headers)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused(400, f"the body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise Refused(400, "the body is not a JSON object")
    event = _header(headers, EVENT_HEADERS).lower()
    event_type = _header(headers, TYPE_HEADERS).lower()
    delivery = _header(headers, DELIVERY_HEADERS)
    label = event_type or event or "?"

    def ignored(reason: str) -> dict:
        _record(name, label, "ignored: " + reason)
        return {"status": 202, "ignored": reason, "event": label}

    told = describe(event, event_type, payload)
    if told is None:
        return ignored(f"nothing to tell a thread about a {label} event"
                       + (f" ({payload['action']})" if payload.get("action") else ""))
    tid = thread_of(payload)
    if tid is None:
        return ignored("no thread:<id> label on the pull request or issue, and no board thread link in its body")
    root = db.get_message(tid)
    if root is None:
        return ignored(f"thread {tid} does not exist on this board")
    tid = root["thread_id"] or root["id"]
    repo = (payload.get("repository") or {}).get("full_name") or ""
    obj = payload.get("pull_request") or payload.get("issue") or {}
    url = obj.get("html_url") or ""
    what, excerpt = told
    body = f"Forgejo: {what}" + (f" {url}" if url else "") + (f"\n> {excerpt}" if excerpt else "")
    meta = {"kind": "forgejo", "event": label, "action": payload.get("action") or "", "repo": repo,
            "number": payload.get("number") or obj.get("number"), "url": url, "delivery": delivery}
    by = progress.board_agent()
    try:
        msg = db.post_message(root["board"], by["id"], body, reply_to=tid, meta=meta,
                              client_key=f"{name}:{delivery}" if delivery else None)
    except PermissionError as exc:        # an archived channel
        return ignored(str(exc))
    if msg.pop("replayed", False):
        _record(name, label, f"replayed: message {msg['id']} in thread {tid}")
        return {"status": 200, "replayed": True, "message": msg["id"], "thread": tid}
    _record(name, label, f"posted message {msg['id']} in thread {tid}")
    return {"status": 201, "message": msg["id"], "thread": tid}
