"""Attachment bytes live in the file store, and a board from before moves there on start (#390)."""
import base64
import io
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from PIL import Image
from app import db, main, storage

def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (1, 1)).save(out, "PNG")
    return out.getvalue()


PNG = _png()


class Base(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(db, "DB_PATH", os.path.join(self.tmp, "board.db")))
        self.stack.enter_context(patch.object(db, "_local", threading.local()))
        self.stack.enter_context(patch.object(main, "READ_TOKEN_REQUIRED", False))
        os.environ.pop("BOT_BOARD_ATTACHMENTS_DIR", None)

    def files(self):
        root = Path(self.tmp, "attachments")
        return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


class Store(Base):
    def test_write_is_addressed_by_content_and_leaves_no_temp_file(self):
        key = storage.write(b"same")
        self.assertEqual(storage.write(b"same"), key)
        self.assertEqual(Path(storage.path(key)).read_bytes(), b"same")
        self.assertEqual(self.files(), [Path(self.tmp, "attachments", key[:2], key[2:4], key)])

    def test_write_syncs_the_file_and_every_directory_it_may_have_made(self):
        with patch("os.fsync") as fsync:
            storage.write(b"x")
        self.assertEqual(fsync.call_count, 5)

    def test_env_var_moves_the_directory(self):
        other = os.path.join(self.tmp, "elsewhere")
        with patch.dict(os.environ, {"BOT_BOARD_ATTACHMENTS_DIR": other}):
            self.assertTrue(storage.path(storage.write(b"x")).startswith(other + os.sep))
        self.assertEqual(self.files(), [])

    def test_failed_write_leaves_nothing(self):
        with patch("os.fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
            storage.write(b"x")
        self.assertEqual(self.files(), [])


class Messages(Base):
    def setUp(self):
        super().setUp()
        self.client = self.stack.enter_context(TestClient(main.app))
        token = self.client.post("/api/agents", json={"handle": "writer", "kind": "human"}).json()["token"]
        self.auth = {"Authorization": "Bearer " + token}

    def post(self, **kw):
        r = self.client.post("/api/messages", headers=self.auth, json={"channel": "lobby", "body": "b", **kw})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["message"]["id"]

    def test_delete_keeps_a_file_another_message_still_uses(self):
        first, second = self.post(text_attachment="shared"), self.post(text_attachment="shared")
        self.assertEqual(len(self.files()), 1)
        db.delete_attachments(first)
        self.assertEqual(self.client.get(f"/api/messages/{first}/attachment").status_code, 404)
        self.assertEqual(self.client.get(f"/api/messages/{second}/attachment").content, b"shared")
        db.delete_attachments(second)
        self.assertEqual(self.files(), [])

    def test_a_shared_key_counts_across_both_tables(self):
        text = self.post(text_attachment="a key shared by hand")
        image = self.post(image_attachment=base64.b64encode(PNG).decode())
        key = db.conn().execute("SELECT storage_key FROM image_attachments WHERE message_id=?", (image,)).fetchone()[0]
        with db.conn() as c:
            c.execute("UPDATE text_attachments SET storage_key=? WHERE message_id=?", (key, text))
        db.delete_attachments(text)
        self.assertEqual(self.client.get(f"/api/messages/{image}/image").content, PNG)

    def test_seed_text_goes_through_the_store(self):
        r = self.client.post("/api/workspaces", headers=self.auth, json={
            "slug": "game", "name": "Game", "seed": {"text": "pasted seed"}})
        self.assertLess(r.status_code, 300, r.text)
        tid = r.json()["workspace"]["seed_thread_id"]
        self.assertEqual(self.client.get(f"/api/messages/{tid}/attachment").content, b"pasted seed")

    def test_download_headers_stay_as_they_were(self):
        image = self.post(image_attachment=base64.b64encode(PNG).decode())
        r = self.client.get(f"/api/messages/{image}/image")
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertTrue(r.headers["content-disposition"].startswith("inline; filename="))
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r.headers["content-security-policy"], "default-src 'none'; sandbox")
        self.assertIn("no-store", r.headers["cache-control"])


class Upgrade(Base):
    """A database as the last release left it, with content in the tables."""
    TEXT = " \r\nLong text é \U0001f600\n "

    def setUp(self):
        super().setUp()
        schema = (db.SCHEMA.replace("    storage_key TEXT NOT NULL,\n    size_bytes", "    content TEXT NOT NULL,\n    size_bytes", 1)
                  .replace("    storage_key TEXT NOT NULL,\n    media_type", "    content BLOB NOT NULL,\n    media_type", 1))
        c = sqlite3.connect(db.DB_PATH)
        c.executescript(schema)
        c.execute("INSERT INTO boards (id, slug, created_at) VALUES (1, 'lobby', 'now')")
        c.execute("INSERT INTO agents (id, handle, token_hash, created_at) VALUES (1, 'old', 'h', 'now')")
        for mid in (1, 2, 3):
            c.execute("INSERT INTO messages (id, board_id, agent_id, body, thread_id, created_at)"
                      " VALUES (?, 1, 1, 'b', ?, 'now')", (mid, mid))
        for mid in (1, 2):      # two messages with one text: one file
            c.execute("INSERT INTO text_attachments VALUES (?, ?, ?)", (mid, self.TEXT, len(self.TEXT.encode())))
        c.execute("INSERT INTO image_attachments VALUES (3, ?, 'image/png', 'a.png', ?)", (PNG, len(PNG)))
        c.commit()
        c.close()

    def assert_moved(self):
        client = self.stack.enter_context(TestClient(main.app))
        for mid in (1, 2):
            self.assertEqual(client.get(f"/api/messages/{mid}/attachment").content, self.TEXT.encode())
        self.assertEqual(client.get("/api/messages/3/image").content, PNG)
        self.assertEqual(len(self.files()), 2)
        for table in ("text_attachments", "image_attachments"):
            cols = {r["name"] for r in db.conn().execute(f"PRAGMA table_info({table})")}
            self.assertNotIn("content", cols)
            self.assertIn("storage_key", cols)

    def test_upgrade_moves_every_attachment_byte_identical(self):
        self.assert_moved()

    def test_a_crash_midway_is_safe_to_run_again(self):
        real, calls = storage.write, []

        def dies(content):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("power cut")
            return real(content)
        with patch.object(storage, "write", dies), self.assertRaises(OSError):
            db.init()
        row = db.conn().execute("SELECT storage_key, content FROM text_attachments WHERE message_id=2").fetchone()
        self.assertIsNone(row["storage_key"])
        self.assertEqual(row["content"], self.TEXT)
        self.assert_moved()

    def test_a_second_start_changes_nothing(self):
        self.assert_moved()
        before = self.files()
        db.init()
        self.assertEqual(self.files(), before)



if __name__ == "__main__":
    unittest.main()
