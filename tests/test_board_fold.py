"""The thread panel folds consecutive @board messages into one strip when the viewer asks (#403).
The grouping is pure JavaScript between the `fold:begin` and `fold:end` markers of the app script;
this runs it under node with sample messages. Needs node; skipped without it."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest

from app import web

NODE = shutil.which("node")


def fold_js() -> str:
    m = re.search(r"// fold:begin\n(.*?)// fold:end", web.APP_JS, re.S)
    assert m, "the fold block is missing its markers"
    return m.group(1)


def run(messages, pref, me):
    script = fold_js() + """
var r = foldRuns(MSGS, PREF, ME).map(function(seg){
  return seg.fold ? {fold: seg.fold.map(function(m){ return m.id; }), summary: foldSummary(seg.fold)} : {msg: seg.msg.id};
});
process.stdout.write(JSON.stringify(r));
"""
    script = ("var MSGS=%s, PREF=%s, ME=%s;\n" % (json.dumps(messages), json.dumps(pref), json.dumps(me))) + script
    with tempfile.NamedTemporaryFile("w", suffix=".js") as fh:
        fh.write(script)
        fh.flush()
        out = subprocess.run([NODE, fh.name], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def board(mid, event, action, body="", needs=None, mentions=()):
    meta = {"kind": "forgejo", "event": event, "action": action} if event else {}
    if needs:
        meta["needs"] = needs
    return {"id": mid, "author": "board", "reply_to": 1, "body": body, "meta": meta, "mentions": list(mentions)}


def reply(mid, author, mentions=()):
    return {"id": mid, "author": author, "reply_to": 1, "body": "hi", "meta": {}, "mentions": list(mentions)}


@unittest.skipUnless(NODE, "node is not installed")
class BoardFold(unittest.TestCase):
    def test_visible_keeps_every_card(self):
        msgs = [board(2, "pull_request", "opened"), board(3, "pull_request_label", "label_updated"), reply(4, "jac")]
        self.assertEqual(run(msgs, "visible", "jac"), [{"msg": 2}, {"msg": 3}, {"msg": 4}])

    def test_consecutive_board_messages_fold_into_one_strip(self):
        msgs = [reply(2, "rufus"),
                board(3, "pull_request", "opened"), board(4, "pull_request_label", "label_updated"),
                board(5, "pull_request_comment", "created"), board(6, "pull_request_sync", "synchronized"),
                reply(7, "jac"),
                board(8, "pull_request", "closed", body="Forgejo: jac merged PR #9")]
        self.assertEqual(run(msgs, "fold", "jac"), [
            {"msg": 2},
            {"fold": [3, 4, 5, 6], "summary": "1 PR, 1 labels, 1 comment, 1 push"},
            {"msg": 7},
            {"fold": [8], "summary": "1 merge"},
        ])

    def test_plurals_in_the_summary(self):
        msgs = [board(2, "pull_request", "opened"), board(3, "pull_request", "opened"),
                board(4, "issue_comment", "created"), board(5, "issue_comment", "created"), board(6, None, None)]
        self.assertEqual(run(msgs, "fold", None)[0]["summary"], "2 PRs, 2 comments, 1 note")

    def test_a_message_for_the_viewer_is_never_folded(self):
        msgs = [board(2, "pull_request", "opened"),
                board(3, None, None, body="@jac this waits on you", needs="jac"),
                board(4, "pull_request", "opened"),
                board(5, None, None, body="@jac a nudge", mentions=["jac"]),
                board(6, "pull_request", "opened")]
        self.assertEqual(run(msgs, "fold", "jac"), [
            {"fold": [2], "summary": "1 PR"}, {"msg": 3}, {"fold": [4], "summary": "1 PR"}, {"msg": 5}, {"fold": [6], "summary": "1 PR"}])
        # another viewer, or nobody signed in, sees them folded with the rest
        self.assertEqual(run(msgs, "fold", "milo"), [{"fold": [2, 3, 4, 5, 6], "summary": "3 PRs, 2 notes"}])
        self.assertEqual(run(msgs, "fold", None), [{"fold": [2, 3, 4, 5, 6], "summary": "3 PRs, 2 notes"}])

    def test_the_root_is_always_a_card(self):
        root = {"id": 1, "author": "board", "reply_to": None, "body": "a sweeper note", "meta": {}, "mentions": []}
        self.assertEqual(run([root, board(2, None, None)], "fold", None), [{"msg": 1}, {"fold": [2], "summary": "1 note"}])

    def test_the_controls_are_on_the_page(self):
        page = web.app_page("b", "", [], [], [], {"cursor": 0})
        self.assertEqual(page.count('name="boardmsgs-thread"'), 2)   # the display menu in the thread panel header
        self.assertEqual(page.count('name="boardmsgs-nav"'), 2)      # the same radio under menu ▾
        self.assertIn('id="tdisplay"', page)

if __name__ == "__main__":
    unittest.main()
