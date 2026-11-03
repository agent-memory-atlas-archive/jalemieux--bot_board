"""Run the suite in parallel processes (#416): the same tests `python -m unittest discover -s tests`
finds, a test class per process, and a large class split a test per process. Most of the suite's time
is tests waiting on the real launcher to settle, so the waits overlap instead of adding up.
Run with: python tests/parallel.py [-j N]"""
import argparse
import os
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENV = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [HERE, os.environ.get("PYTHONPATH")])))
SPLIT = 20      # a class with more tests than this runs a test per process


def cases(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from cases(t)
        else:
            yield t


def units():
    """Test ids grouped into the units a process runs: a whole class, or one test of a large class."""
    by_class = {}
    for t in cases(unittest.defaultTestLoader.discover(HERE)):
        if type(t).__name__ == "_FailedTest":       # an import error: run it alone, it reports itself
            by_class[t.id()] = [t.id()]
            continue
        by_class.setdefault(t.id().rsplit(".", 1)[0], []).append(t.id())
    for cls, ids in by_class.items():
        if len(ids) > SPLIT:
            yield from ([i] for i in ids)
        else:
            yield [cls]


def run(ids):
    start = time.monotonic()
    out = subprocess.run([sys.executable, "-m", "unittest", *ids], cwd=ROOT, env=ENV, capture_output=True, text=True)
    return ids, out, time.monotonic() - start


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", type=int, default=os.cpu_count() or 4, help="processes at once (default: the cores)")
    args = ap.parse_args()
    sys.path[:0] = [HERE, ROOT]
    todo = sorted(units())
    start, failed, ran = time.monotonic(), [], 0
    with ThreadPoolExecutor(args.j) as pool:
        for ids, out, took in pool.map(run, todo):
            tail = out.stderr.strip().splitlines()
            ran += next((int(line.split()[1]) for line in tail if line.startswith("Ran ")), 0)
            if out.returncode:
                failed.append(" ".join(ids))
                print(out.stdout + out.stderr, flush=True)
            elif took > 20:
                print("slow: %s %.0fs" % (" ".join(ids), took), flush=True)
    print("Ran %d tests in %d processes, %.1fs" % (ran, len(todo), time.monotonic() - start))
    if failed:
        print("FAILED:\n  " + "\n  ".join(failed))
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
