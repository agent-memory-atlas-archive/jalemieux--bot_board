"""Attachment bytes on disk, addressed by their SHA-256, so identical uploads share a file.

The database says which message has which attachment; this store only keeps bytes. Files
live beside the database in `attachments/` unless BOT_BOARD_ATTACHMENTS_DIR says otherwise.
"""
from __future__ import annotations

import hashlib
import os
import tempfile


def _root() -> str:
    from . import db
    return os.environ.get("BOT_BOARD_ATTACHMENTS_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(db.DB_PATH)), "attachments")


def path(key: str) -> str:
    """Where a key's file is. The key is the content's hex digest, never client input."""
    return os.path.join(_root(), key[:2], key[2:4], key)


def write(content: bytes) -> str:
    """Store the bytes and return their key. On return the file survives a crash, so a row
    committed after this call never points at nothing."""
    key = hashlib.sha256(content).hexdigest()
    target = path(key)
    if os.path.exists(target):
        return key
    folder = os.path.dirname(target)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        os.unlink(tmp)
        raise
    dirfd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
    return key


def delete(key: str) -> None:
    """Remove a key's file. Other rows may share a key: see db.delete_attachments."""
    try:
        os.unlink(path(key))
    except FileNotFoundError:
        pass
