"""deploy/release.sh --auto deploys the newest release tag that has an image (#430): a tag whose
release run was cancelled, or is still building, has none and is passed over, and nothing older
than what is live is ever deployed. Runs the real script with fake git, docker and curl on PATH.
Run with python -m unittest discover -s tests."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# tags.txt: the tags on Forgejo; images.txt: the versions the registry has; live: what /healthz says.
FAKES = {
    "git": """#!/bin/sh
[ "$1" = ls-remote ] || exit 0
while read -r t; do echo "0000 refs/tags/$t"; done < "$FAKE/tags.txt"
""",
    "curl": """#!/bin/sh
v=$(cat "$FAKE/live" 2>/dev/null); [ -n "$v" ] || exit 7
echo "{\\"ok\\":true,\\"version\\":\\"$v\\"}"
""",
    "docker": """#!/bin/sh
echo "$*" >> "$FAKE/docker.log"
case "$1 $2" in
  "image inspect") exit 1 ;;
  "pull -q") v=${3##*:}; grep -qx "$v" "$FAKE/images.txt"; exit $? ;;
  "tag "*) case "$3" in *:latest) v=${2##*:}; echo "$v" > "$FAKE/next" ;; esac ;;
  "compose up") cp "$FAKE/next" "$FAKE/live" ;;
  "images --format") cat "$FAKE/live" ;;
  "inspect -f")
    case "$3" in
      *State.Health*) echo healthy ;;
      *Config.Env*) echo "BOT_BOARD_VERSION=${4##*:}" ;;
      *) echo sha256:live ;;
    esac ;;
esac
exit 0
""",
}


class ReleaseAuto(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.fake = Path(tmp) / "fake"
        (Path(tmp) / "bin").mkdir()
        self.fake.mkdir()
        for name, body in FAKES.items():
            f = Path(tmp) / "bin" / name
            f.write_text(body)
            f.chmod(0o755)
        (Path(tmp) / "deploy").mkdir()
        shutil.copy(ROOT / "deploy" / "release.sh", Path(tmp) / "deploy" / "release.sh")
        self.script = Path(tmp) / "deploy" / "release.sh"
        self.env = dict(os.environ, FAKE=str(self.fake), PATH=f"{tmp}/bin:{os.environ['PATH']}",
                        BOT_BOARD_RELEASE_TOKEN_FILE=str(Path(tmp) / "no-token"))

    def run_auto(self, tags, images, live):
        (self.fake / "tags.txt").write_text("".join(t + "\n" for t in tags))
        (self.fake / "images.txt").write_text("".join(t + "\n" for t in images))
        (self.fake / "live").write_text(live)
        r = subprocess.run(["bash", str(self.script), "--auto"], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return (self.fake / "live").read_text().strip(), r.stdout

    def test_a_cancelled_newest_tag_is_passed_over(self):
        live, out = self.run_auto(["v1.0.206", "v1.0.207", "v1.0.208"], ["v1.0.206", "v1.0.207"], "v1.0.206")
        self.assertEqual(live, "v1.0.207", out)

    def test_the_newest_tag_with_an_image_is_deployed(self):
        live, out = self.run_auto(["v1.0.206", "v1.0.207", "v1.0.208"], ["v1.0.206", "v1.0.207", "v1.0.208"], "v1.0.206")
        self.assertEqual(live, "v1.0.208", out)

    def test_nothing_older_than_live_is_deployed(self):
        # v1.0.209 has no image and v1.0.208 is live: v1.0.207 below it is never pulled
        live, out = self.run_auto(["v1.0.207", "v1.0.208", "v1.0.209"], ["v1.0.207", "v1.0.208"], "v1.0.208")
        self.assertEqual(live, "v1.0.208", out)
        self.assertNotIn("v1.0.207", (self.fake / "docker.log").read_text())

    def test_nothing_live_the_newest_tag_with_an_image_is_deployed(self):
        live, out = self.run_auto(["v1.0.207", "v1.0.208"], ["v1.0.207"], "")
        self.assertEqual(live, "v1.0.207", out)

    def test_live_is_newest_nothing_is_pulled(self):
        live, _ = self.run_auto(["v1.0.207", "v1.0.208"], ["v1.0.207", "v1.0.208"], "v1.0.208")
        self.assertEqual(live, "v1.0.208")
        self.assertFalse((self.fake / "docker.log").exists())


if __name__ == "__main__":
    unittest.main()
