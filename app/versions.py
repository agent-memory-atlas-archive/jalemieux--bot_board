"""What runs across the fleet against what should (#239, docs/fleet-updates.md): the board,
each keeper, each bot's launcher and session, and each recipe routine. The keepers send the
facts (#238); this compares them, for the Versions section on /fleet and the bell."""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOARD_VERSION = os.environ.get("BOT_BOARD_VERSION", "dev")
BOARD_COMMIT = os.environ.get("BOT_BOARD_COMMIT", "")      # set by deploy/release.sh; empty for ad-hoc builds
# Below this a keeper or a launcher is flagged: bump it when the board stops working with an older one.
MIN_PROTOCOL = 1
UP = ("waiting", "running")
FIX_KEEPER = "git -C <checkout> pull && bot keeper install"


def _constant(path: Path, pattern: str) -> int | None:
    """An integer constant from bin/bot or bin/keeper as shipped with the board, or None."""
    try:
        m = re.search(pattern, path.read_text(encoding="utf-8"), re.M)
    except OSError:
        return None
    return int(m.group(1)) if m else None


SESSION_GEN = _constant(ROOT / "bin" / "bot", r"^SESSION_GEN=(\d+)")
LAUNCHER_PROTOCOL = _constant(ROOT / "bin" / "bot", r"^PROTOCOL=(\d+)") or 3


def _row(what, name, running, wanted="", note="", href="", bell=False):
    return {"what": what, "name": name, "running": running, "wanted": wanted, "note": note,
            "href": href, "bell": bell, "current": not note}


def rows(hosts: list, routines: list = ()) -> list[dict]:
    """One row per piece that runs: {what, name, running, wanted, note, href, bell, current}.
    note says why it is not what should run; bell is set where only a person can fix it."""
    out = [_row("board", "board", f"{BOARD_VERSION}" + (f" ({BOARD_COMMIT[:12]})" if BOARD_COMMIT else ""), "main")]
    for h in hosts:
        if not h.get("reporting"):
            continue
        href, u = f"/fleet#box-{h['name']}", h.get("update")
        k = h["keeper"] if isinstance(h.get("keeper"), dict) else {}
        if u is None:
            out.append(_row("keeper", h["name"], "unknown", BOARD_COMMIT[:12],
                            f"predates self-update (#107): on {h['name']}, run `{FIX_KEEPER}`", href, True))
        elif not k.get("loaded"):
            out.append(_row("keeper", h["name"], f"checkout {u.get('revision') or '?'}", BOARD_COMMIT[:12],
                            f"a keeper from before #238 (protocol below {MIN_PROTOCOL}): it updates itself"
                            " within minutes; if not, " + FIX_KEEPER, href))
        else:
            note = ""
            if (k.get("protocol") or 0) < MIN_PROTOCOL:
                note = f"protocol {k.get('protocol')} is below {MIN_PROTOCOL}: run `{FIX_KEEPER}`"
            elif BOARD_COMMIT and k["loaded"] != BOARD_COMMIT:
                note = "not the board's commit yet: the keeper moves to it at its next update check (#240)"
            out.append(_row("keeper", h["name"], f"{k['loaded'][:12]} since {k.get('started') or '?'}",
                            BOARD_COMMIT[:12], note, href, note.startswith("protocol")))
        for b in h.get("bots") or []:
            if not (b.get("status") or "").startswith(UP):
                continue
            href = f"/fleet#bot-{b['handle']}"
            launcher = b.get("launcher") or ""
            if b.get("restartable") is False:
                note = "predates bot restart (#107): the keeper stops and starts it at its next restart"
            elif b.get("behind"):
                note = "behind the keeper's checkout: it restarts on it once idle"
            elif b.get("protocol") is not None and b["protocol"] < MIN_PROTOCOL:
                note = f"protocol {b['protocol']} is below {MIN_PROTOCOL}"
            elif (b.get("protocol") or 0) < LAUNCHER_PROTOCOL:
                note = (f"predates board-only skills (protocol {LAUNCHER_PROTOCOL}): it writes its profile's skills from"
                        " the board once it restarts on the new launcher")
            else:
                note = ""
            out.append(_row("launcher", b["handle"], launcher or "no version file", "the keeper's checkout", note, href))
            gen = b.get("session_gen") or ""
            if SESSION_GEN is not None and "session_gen" in b:
                out.append(_row("session", b["handle"], f"generation {gen or 'unrecorded'}", f"generation {SESSION_GEN}",
                                "" if gen == str(SESSION_GEN) else "started under an older first prompt:"
                                " a new session at its next start", href))
    try:
        from . import routines as routines_mod
        current = routines_mod.cli.RECIPE_VERSION
    except Exception:       # bin/routines missing or broken: nothing to compare with
        current = None
    for r in routines:
        v = r.get("recipe_version")
        if v is None or current is None:
            continue
        out.append(_row("routine", r["name"], f"recipe v{v}", f"recipe v{current}",
                        "" if v >= current else "an older recipe: a new version needs a human's yes",
                        f"/routines#r={r['id']}"))
    return out


def attention(rows: list) -> list[tuple[str, str]]:
    """(text, href) for the bell: what only a person can fix. The rest catches up on its own
    (keepers follow main, launchers restart once idle, sessions renew at their next start)."""
    return [(f"keeper on {r['name']}: {r['note']}", r["href"]) for r in rows if r["bell"]]
