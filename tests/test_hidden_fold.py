"""A hidden thread folds into a strip in the channel stream, consecutive ones into one strip (#407).
The grouping is pure JavaScript between the `hidefold:begin` and `hidefold:end` markers of the app
script; this runs it under node with sample roots. Needs node; skipped without it."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest

from app import web

NODE = shutil.which("node")


def fold_js() -> str:
    m = re.search(r"// hidefold:begin\n(.*?)// hidefold:end", web.APP_JS, re.S)
    assert m, "the hidden-thread fold block is missing its markers"
    return m.group(1)


def run(roots):
    script = ("var ROOTS=%s;\n" % json.dumps(roots)) + fold_js() + """
var r = hiddenRuns(ROOTS).map(function(seg){
  return seg.hidden ? {hidden: seg.hidden.map(function(m){ return m.id; }), summary: hiddenSummary(seg.hidden)} : {msg: seg.msg.id};
});
process.stdout.write(JSON.stringify(r));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js") as fh:
        fh.write(script)
        fh.flush()
        out = subprocess.run([NODE, fh.name], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def root(mid, body="a thread", hidden=False, replies=0):
    m = {"id": mid, "reply_to": None, "body": body, "replies": {"count": replies, "authors": []} if replies else None}
    if hidden:
        m.update(hidden=True, hidden_by="jac", hidden_at="2026-11-03T10:09:00+00:00")
    return m


@unittest.skipUnless(NODE, "node is not installed")
class HiddenFold(unittest.TestCase):
    def test_visible_threads_stay_cards(self):
        self.assertEqual(run([root(1), root(2), root(3)]), [{"msg": 1}, {"msg": 2}, {"msg": 3}])

    def test_a_hidden_thread_folds_in_its_place_with_id_first_words_and_replies(self):
        roots = [root(1), root(2, "  Need   some QoL improvements for humans on the board, please read ", hidden=True, replies=3), root(3)]
        self.assertEqual(run(roots), [
            {"msg": 1},
            {"hidden": [2], "summary": "#2 Need some QoL improvements for humans on the… · 3 replies"},
            {"msg": 3}])
        self.assertEqual(run([root(4, "short", hidden=True, replies=1)])[0]["summary"], "#4 short · 1 reply")
        self.assertEqual(run([root(5, "none yet", hidden=True)])[0]["summary"], "#5 none yet · 0 replies")

    def test_consecutive_hidden_threads_fold_into_one_strip(self):
        roots = [root(1, hidden=True), root(2, hidden=True), root(3, hidden=True), root(4), root(5, hidden=True)]
        self.assertEqual(run(roots), [{"hidden": [1, 2, 3], "summary": "3 hidden threads"}, {"msg": 4},
                                      {"hidden": [5], "summary": "#5 a thread · 0 replies"}])


if __name__ == "__main__":
    unittest.main()
