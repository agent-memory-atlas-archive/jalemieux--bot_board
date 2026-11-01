"""Images are bounded, atomically attached, and fetched outside message feeds."""
import base64
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from urllib.parse import urlsplit
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, images, main
from client.board_client import Board, main as client_main


def picture(format="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format=format)
    return buffer.getvalue()


class ImageAttachments(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(db, "DB_PATH", os.path.join(tmp, "board.db")))
        self.stack.enter_context(patch.object(db, "_local", threading.local()))
        self.stack.enter_context(patch.object(main, "READ_TOKEN_REQUIRED", False))
        self.client = self.stack.enter_context(TestClient(main.app))
        self.identity = self.client.post("/api/agents", json={"handle": "operator", "kind": "human"}).json()
        self.auth = {"Authorization": "Bearer " + self.identity["token"]}

    def tearDown(self):
        if hasattr(db._local, "conn"):
            db._local.conn.close()

    def post(self, content=None, **kw):
        payload = {"channel": "lobby", "body": "Screenshot", **kw}
        if content is not None:
            payload["image_attachment"] = base64.b64encode(content).decode("ascii")
        return self.client.post("/api/messages", headers=self.auth, json=payload)

    def test_formats_persistence_and_small_feeds(self):
        reader = self.client.post("/api/agents", json={"handle": "reader"}).json()
        root = self.post().json()["message"]
        for format, (media_type, extension) in images.FORMATS.items():
            with self.subTest(format=format):
                content = picture(format)
                response = self.post(content, body="@reader Screenshot", reply_to=root["id"],
                                     text_attachment="Notes for the screenshot")
                self.assertEqual(response.status_code, 201, response.text)
                message = response.json()["message"]
                attachment = message["attachments"][1]
                self.assertEqual(attachment["name"], "image." + extension)
                self.assertEqual(attachment["size_bytes"], len(content))
                download = self.client.get(attachment["url"])
                self.assertEqual(download.content, content)
                self.assertEqual(download.headers["content-type"], media_type)
                self.assertIn("sandbox", download.headers["content-security-policy"])
                self.assertEqual(download.headers["x-content-type-options"], "nosniff")
                self.assertIn("no-store", download.headers["cache-control"])
                for path in ("/api/messages", "/api/inbox", f'/api/threads/{root["id"]}', "/api/search"):
                    for view in ("full", "compact"):
                        response = self.client.get(path, params={"view": view, "q": "Screenshot"},
                                                   headers={"Authorization": "Bearer " + reader["token"]})
                        self.assertEqual(response.status_code, 200, response.text)
                        found = next(m for m in response.json()["messages"] if m["id"] == message["id"])
                        self.assertEqual(found["attachments"], message["attachments"])
                        self.assertNotIn("image_attachment", response.text)
                        self.assertLess(len(response.content), main.MAX_RESPONSE_BYTES)
                with sqlite3.connect(db.DB_PATH) as connection:
                    self.assertEqual(connection.execute(
                        "SELECT content FROM image_attachments WHERE message_id=?", (message["id"],)
                    ).fetchone()[0], content)
                page = self.client.get("/search?q=Screenshot").text
                self.assertIn(f'data-image-src="{attachment["url"]}"', page)
        self.assertEqual(self.client.get("/api/messages/999999/image").status_code, 404)
        self.assertEqual(self.client.get(f'/api/messages/{root["id"]}/image').status_code, 404)
        self.assertIn("image_attachment", self.client.get("/agents.md").text)
        self.assertIn(f'"max_image_attachment_bytes": {images.MAX_BYTES}', self.client.get("/").text)

    def test_private_reads_and_conversation_post_permissions(self):
        message = self.post(picture()).json()["message"]
        url = message["attachments"][0]["url"]
        with patch.object(main, "READ_TOKEN_REQUIRED", True):
            self.assertEqual(self.client.get(url).status_code, 401)
            self.assertEqual(self.client.get(url, headers={"Authorization": "Bearer invalid"}).status_code, 401)
            self.assertEqual(self.client.get(url, headers=self.auth).content, picture())
        other = self.client.post("/api/agents", json={"handle": "other"}).json()
        third = self.client.post("/api/agents", json={"handle": "third"}).json()
        conversation = self.client.post("/api/conversations", headers=self.auth,
                                        json={"participants": ["other"]}).json()["conversation"]
        response = self.client.post("/api/messages", headers={"Authorization": "Bearer " + third["token"]},
                                    json={"channel": conversation["slug"], "body": "No permission",
                                          "image_attachment": base64.b64encode(picture()).decode()})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM image_attachments").fetchone()[0], 1)

    def test_reject_invalid_active_oversized_and_high_pixel_images(self):
        before = db.cursor()
        for content in (b"", b"not an image", b"<svg xmlns='http://www.w3.org/2000/svg'/>", picture()[:20]):
            self.assertEqual(self.post(content).status_code, 422)
        for other in ("TIFF", "BMP"):     # no parser but the four allowed ever sees the bytes (#338)
            raw = io.BytesIO(); Image.new("RGB", (2, 2)).save(raw, other)
            response = self.post(raw.getvalue())
            self.assertEqual(response.status_code, 422)
            self.assertIn("only PNG, JPEG, GIF and WebP", response.text)
        for encoded in ("???", "data:image/png;base64," + base64.b64encode(picture()).decode()):
            self.assertEqual(self.post(image_attachment=encoded).status_code, 422)
        with patch.object(images, "MAX_PIXELS", 3):
            self.assertEqual(self.post(picture()).status_code, 422)
        self.assertEqual(self.post(b"x" * (images.MAX_BYTES + 1)).status_code, 422)
        self.assertEqual(db.cursor(), before)
        exact = picture() + b"\0" * (images.MAX_BYTES - len(picture()))
        response = self.post(exact)
        self.assertEqual(response.status_code, 201, response.text[:500])
        self.assertEqual(response.json()["message"]["attachments"][0]["size_bytes"], images.MAX_BYTES)
        self.assertEqual(self.post(exact + b"\0").status_code, 422)
        self.assertEqual(self.post(picture(), body=" ").status_code, 422)

    def test_failed_posts_are_atomic_and_old_database_upgrades(self):
        before = db.cursor()
        self.assertEqual(self.post(picture(), reply_to=999999).status_code, 404)
        self.assertEqual(db.cursor(), before)
        db.conn().execute("""CREATE TRIGGER fail_image BEFORE INSERT ON image_attachments
                             BEGIN SELECT RAISE(ABORT, 'image write failed'); END""")
        db.conn().commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "image write failed"):
            self.post(picture(), text_attachment="Not saved")
        self.assertEqual(db.cursor(), before)
        self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM text_attachments").fetchone()[0], 0)
        db.conn().execute("DROP TRIGGER fail_image")
        db.conn().execute("DROP TABLE image_attachments")
        db.conn().commit()
        db.init()
        message = self.post(picture()).json()["message"]
        with db.conn() as connection:
            connection.execute("DELETE FROM messages WHERE id=?", (message["id"],))
        self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM image_attachments").fetchone()[0], 0)

    def test_sdk_and_cli_encode_images_without_changing_ordinary_posts(self):
        with patch.object(Board, "_call", return_value={"message": {"id": 12}}) as call:
            board = Board(token="test", token_file="/nonexistent")
            board.say("Screenshot", image=picture())
            self.assertEqual(base64.b64decode(call.call_args.args[2]["image_attachment"]), picture())
            board.say("Ordinary")
            self.assertNotIn("image_attachment", call.call_args.args[2])
            with tempfile.NamedTemporaryFile() as file:
                file.write(picture())
                file.flush()
                with patch.object(sys, "argv", ["board", "say", "Screenshot", "--image", file.name, "--reply-to", "7"]), \
                        patch("sys.stdout", new_callable=io.StringIO):
                    client_main()
                payload = call.call_args.args[2]
                self.assertEqual(payload["reply_to"], 7)
                self.assertEqual(base64.b64decode(payload["image_attachment"]), picture())

    def test_browser_drop_reply_preview_and_failure_recovery(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Install Playwright and Chromium for image UI checks")
        with sync_playwright() as playwright, ExitStack() as cleanup:
            browser = playwright.chromium.launch()
            cleanup.callback(browser.close)
            page = browser.new_page()
            errors, posts, image_auth = [], [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            fail_post, fail_image = [True], [True]

            def serve(route):
                request = route.request
                path = request.url.replace("http://board.test", "")
                endpoint = urlsplit(path).path
                if "wait=30" in path:
                    route.abort()
                    return
                if endpoint.endswith("/image"):
                    image_auth.append(request.headers.get("authorization"))
                    if fail_image[0]:
                        fail_image[0] = False
                        route.fulfill(status=503, json={"detail": "image unavailable"})
                        return
                if request.method == "POST" and endpoint == "/api/messages":
                    posts.append(json.loads(request.post_data))
                    if fail_post[0]:
                        fail_post[0] = False
                        route.fulfill(status=503, json={"detail": "posting unavailable"})
                        return
                response = self.client.request(request.method, path, content=request.post_data, headers=request.headers)
                route.fulfill(status=response.status_code, body=response.content,
                              content_type=response.headers.get("content-type", "text/plain"))

            page.route("**/*", serve)
            identity = {"handle": "operator", "token": self.identity["token"]}
            page.add_init_script("localStorage.setItem('bb.me', " + json.dumps(json.dumps(identity)) + ")")
            # A board no longer creates #lobby by itself (workspaces, #187): make it here.
            self.assertEqual(self.client.post("/api/channels", headers=self.auth,
                                              json={"slug": "lobby", "topic": ""}).status_code, 201)
            page.goto("http://board.test/c/lobby")
            form = page.locator("#compose")
            form.locator("textarea").fill("Screenshot draft")
            form.locator(".image-file").set_input_files(
                {"name": "active.svg", "mimeType": "image/svg+xml", "buffer": b"<svg/>"})
            form.locator(".err").filter(has_text="Choose a PNG").wait_for()
            self.assertEqual(posts, [])
            page.locator("#compose").evaluate("""(form, data) => {
                const transfer = new DataTransfer();
                transfer.items.add(new File([Uint8Array.from(atob(data), c => c.charCodeAt(0))],
                                           'screenshot.png', {type: 'image/png'}));
                form.dispatchEvent(new DragEvent('drop', {dataTransfer: transfer, bubbles: true}));
            }""", base64.b64encode(picture()).decode())
            form.locator(".image-note").filter(has_text="screenshot.png").wait_for()
            form.locator(".send").click()
            form.locator(".err").filter(has_text="posting unavailable").wait_for()
            self.assertEqual(form.locator("textarea").input_value(), "Screenshot draft")
            self.assertIn("screenshot.png", form.locator(".image-note").inner_text())
            with patch.object(main, "READ_TOKEN_REQUIRED", True):
                form.locator(".send").click()
                image = page.locator("#stream img.attached-image").first
                image.wait_for()
                image.scroll_into_view_if_needed()     # images load when they come into view
                page.wait_for_function("document.querySelector('#stream img.attached-image').alt.includes('503')")
                image.focus()
                page.keyboard.press("Enter")
                page.wait_for_function("document.querySelector('#stream img.attached-image').naturalWidth === 2")
                self.assertEqual(posts[0], posts[1])
                self.assertEqual(image_auth, [self.auth["Authorization"]] * 2)
                self.assertEqual(form.locator("textarea").input_value(), "")
                self.assertEqual(form.locator(".image-note").inner_text(), "")
            root = db.list_messages("lobby", 0, None, 20)[-1]
            page.goto(f'http://board.test/t/{root["id"]}')
            reply = page.locator("#tcompose")
            reply.locator(".image-file").set_input_files(
                {"name": "reply.png", "mimeType": "image/png", "buffer": picture()})
            reply.locator(".image-note").filter(has_text="reply.png").wait_for()
            reply.locator(".remove-image").click()
            self.assertEqual(reply.locator(".image-note").inner_text(), "")
            reply.locator(".image-file").set_input_files(
                {"name": "reply.png", "mimeType": "image/png", "buffer": picture()})
            reply.locator(".image-note").filter(has_text="reply.png").wait_for()
            with page.expect_response(lambda r: r.request.method == "POST" and
                                      urlsplit(r.url).path == "/api/messages"):
                reply.locator(".send").click()
            messages = self.client.get(f'/api/threads/{root["id"]}', headers=self.auth).json()["messages"]
            self.assertEqual(messages[-1]["body"], "Image attached.")
            self.assertEqual(messages[-1]["reply_to"], root["id"])
            self.assertEqual(len(messages[-1]["attachments"]), 1)
            self.assertFalse(errors, errors)


if __name__ == "__main__":
    unittest.main()
