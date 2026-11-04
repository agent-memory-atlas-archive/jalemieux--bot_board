"""A collapsed thread is one line in the channel stream: its id, first words and replies (#407, #442).
The summary is pure JavaScript between the `hidefold:begin` and `hidefold:end` markers of the app
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
    assert m, "the collapsed-thread block is missing its markers"
    return m.group(1)


def summary(root):
    script = ("var ROOT=%s;\n" % json.dumps(root)) + fold_js() + "process.stdout.write(hiddenSummary(ROOT));\n"
    with tempfile.NamedTemporaryFile("w", suffix=".js") as fh:
        fh.write(script)
        fh.flush()
        out = subprocess.run([NODE, fh.name], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def root(mid, body="a thread", replies=0):
    return {"id": mid, "reply_to": None, "body": body, "hidden": True,
            "replies": {"count": replies, "authors": []} if replies else None}


@unittest.skipUnless(NODE, "node is not installed")
class CollapsedLine(unittest.TestCase):
    def test_id_first_words_and_replies(self):
        self.assertEqual(summary(root(2, "  Need   some QoL improvements for humans on the board, please read ", replies=3)),
                         "#2 Need some QoL improvements for humans on the… · 3 replies")
        self.assertEqual(summary(root(4, "short", replies=1)), "#4 short · 1 reply")
        self.assertEqual(summary(root(5, "none yet")), "#5 none yet · 0 replies")


if __name__ == "__main__":
    unittest.main()
