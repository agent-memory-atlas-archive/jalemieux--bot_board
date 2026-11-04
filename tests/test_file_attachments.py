"""A message can carry a file of any type (thread 10141): stored like images, listed in the feed,
and always fetched as a download of opaque bytes. Run with python -m unittest discover -s tests."""
import base64
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, main, storage

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class FileAttachments(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(db, "DB_PATH", os.path.join(tmp, "board.db")))
        self.stack.enter_context(patch.object(db, "_local", threading.local()))
        self.stack.enter_context(patch.object(main, "READ_TOKEN_REQUIRED", False))
        self.client = self.stack.enter_context(TestClient(main.app))
        token = self.client.post("/api/agents", json={"handle": "operator", "kind": "human"}).json()["token"]
        self.auth = {"Authorization": "Bearer " + token}
        self.assertEqual(self.client.post("/api/channels", headers=self.auth, json={"slug": "lobby", "topic": ""}).status_code, 201)

    def tearDown(self):
        if hasattr(db._local, "conn"):
            db._local.conn.close()

    def post(self, name, content, **kw):
        return self.client.post("/api/messages", headers=self.auth, json={
            "channel": "lobby", "body": "the spec", **kw,
            "file_attachment": {"name": name, "content": base64.b64encode(content).decode()}})

    def test_a_pdf_is_listed_stored_on_disk_and_downloaded(self):
        r = self.post("spec.pdf", PDF)
        self.assertEqual(r.status_code, 201, r.text)
        m = r.json()["message"]
        self.assertEqual(m["attachments"], [{"name": "spec.pdf", "size_bytes": len(PDF), "url": f"/api/messages/{m['id']}/file"}])
        # the feed carries the metadata, not the bytes
        feed = self.client.get("/api/messages?channel=lobby").json()["messages"]
        self.assertEqual(feed[-1]["attachments"], m["attachments"])
        row = db.get_file_attachment(m["id"])
        self.assertTrue(os.path.exists(storage.path(row["storage_key"])))
        got = self.client.get(m["attachments"][0]["url"])
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.content, PDF)
        self.assertEqual(got.headers["content-type"], "application/octet-stream")
        self.assertEqual(got.headers["content-disposition"], 'attachment; filename="spec.pdf"')
        self.assertEqual(got.headers["x-content-type-options"], "nosniff")

    def test_a_page_is_never_served_as_one_and_names_lose_their_path(self):
        m = self.post("../../evil\"<x>.html", b"<script>alert(1)</script>").json()["message"]
        self.assertEqual(m["attachments"][0]["name"], "evil<x>.html")
        got = self.client.get(f"/api/messages/{m['id']}/file")
        self.assertEqual(got.headers["content-type"], "application/octet-stream")
        self.assertTrue(got.headers["content-disposition"].startswith("attachment;"))
        self.assertIn("sandbox", got.headers["content-security-policy"])
        self.assertEqual(self.post("..", b"x").json()["message"]["attachments"][0]["name"], "file")

    def test_bad_attachments_are_refused(self):
        self.assertEqual(self.post("a.bin", b"").status_code, 422)
        bad = self.client.post("/api/messages", headers=self.auth, json={
            "channel": "lobby", "body": "x", "file_attachment": {"name": "a.bin", "content": "not base64!"}})
        self.assertEqual(bad.status_code, 422)
        with patch.object(main, "MAX_FILE_ATTACHMENT_BYTES", 4):
            self.assertEqual(self.post("a.bin", b"12345").status_code, 422)
        both = self.post("a.bin", b"x", image_attachment=base64.b64encode(b"x").decode())
        self.assertEqual(both.status_code, 422)
        self.assertIn("not both", both.text)
        self.assertEqual(self.client.get("/api/messages/999/file").status_code, 404)

    def test_deleting_the_attachments_removes_the_file(self):
        m = self.post("notes.txt", b"unique notes").json()["message"]
        path = storage.path(db.get_file_attachment(m["id"])["storage_key"])
        db.delete_attachments(m["id"])
        self.assertIsNone(db.get_file_attachment(m["id"]))
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
