"""Tests: run with `venv/bin/python tests.py`.

The bulk-enumeration parser is the part that can fail silently and report
plausible-but-wrong numbers, so most of these tests exist to catch that.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from spacefinder import fastwalk, rules as rules_mod
from spacefinder.fastwalk import DT_DIR, DT_FILE
from spacefinder.scan import Scanner


class TestEngineAgreement(unittest.TestCase):
    """bulk and scandir must be interchangeable."""

    def setUp(self):
        try:
            fastwalk.self_test()
        except fastwalk.SelfTestError as exc:
            self.skipTest(f"bulk engine unavailable: {exc}")

    def test_entries_match_across_many_dirs(self):
        dirs = []
        for base in ("/usr/lib", "/System/Library/Frameworks", "/Applications",
                     os.path.expanduser("~"), "/private/etc"):
            for dirpath, _, _ in os.walk(base):
                dirs.append(dirpath)
                if len(dirs) > 300:
                    break
            if len(dirs) > 300:
                break

        compared = 0
        for d in dirs:
            try:
                bulk = {e.name: e for e in fastwalk.bulk_listdir(d)}
            except OSError:
                continue
            for e in fastwalk.scandir_listdir(d):
                b = bulk.get(e.name)
                if b is None:
                    continue
                self.assertEqual(
                    (b.kind, b.logical, b.alloc, b.nlink, b.ino, b.dev),
                    (e.kind, e.logical, e.alloc, e.nlink, e.ino, e.dev),
                    f"mismatch at {d}/{e.name}")
                compared += 1
        self.assertGreater(compared, 500, "not enough entries compared")

    def test_only_regular_files_carry_bytes(self):
        for e in fastwalk.bulk_listdir("/usr/lib"):
            if e.kind != DT_FILE:
                self.assertEqual(e.alloc, 0, f"{e.name} is not a file")
                self.assertEqual(e.logical, 0, f"{e.name} is not a file")


class TestScanTotals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # 3 dirs x 1 MiB; sizes are exact multiples of the block size so
        # allocated == logical and we can assert on an exact number.
        for sub in ("a", "b", "b/c"):
            os.makedirs(os.path.join(self.tmp, sub), exist_ok=True)
        for sub in ("a", "b", "b/c"):
            with open(os.path.join(self.tmp, sub, "f.bin"), "wb") as fh:
                fh.write(b"\0" * (1024 * 1024))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rollup_matches_du(self):
        res = Scanner(top_files=5).scan([self.tmp])
        out = subprocess.run(["du", "-sk", self.tmp], capture_output=True,
                             text=True).stdout.split("\t")[0]
        self.assertEqual(res.total_alloc[os.path.realpath(self.tmp)],
                         int(out) * 1024)

    def test_nested_totals(self):
        res = Scanner(top_files=5).scan([self.tmp])
        root = os.path.realpath(self.tmp)
        self.assertEqual(res.total_alloc[f"{root}/b"],
                         res.self_alloc[f"{root}/b"]
                         + res.total_alloc[f"{root}/b/c"])
        self.assertEqual(res.file_count, 3)

    def test_engines_agree_on_total(self):
        a = Scanner(engine="bulk", top_files=5).scan([self.tmp])
        b = Scanner(engine="scandir", top_files=5).scan([self.tmp])
        self.assertEqual(a.grand_total, b.grand_total)

    def test_nested_roots_not_double_counted(self):
        res = Scanner(top_files=5).scan([self.tmp, os.path.join(self.tmp, "b")])
        self.assertEqual(len(res.roots), 1)
        single = Scanner(top_files=5).scan([self.tmp])
        self.assertEqual(res.grand_total, single.grand_total)


class TestHardLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        target = os.path.join(self.tmp, "orig.bin")
        with open(target, "wb") as fh:
            fh.write(b"\0" * (1024 * 1024))
        os.link(target, os.path.join(self.tmp, "link.bin"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hard_link_counted_once(self):
        res = Scanner(top_files=5).scan([self.tmp])
        # du counts the inode once; so must we.
        out = subprocess.run(["du", "-sk", self.tmp], capture_output=True,
                             text=True).stdout.split("\t")[0]
        self.assertEqual(res.grand_total, int(out) * 1024)
        self.assertGreater(res.dedup_saved, 0)


class TestStalledDirectory(unittest.TestCase):
    """A directory that blocks forever must not wedge the scan.

    Real cause: a TCC consent check with nobody to answer it, or an
    unresponsive File Provider mount. The syscall cannot be cancelled, so the
    scan has to survive losing the thread.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for sub in ("good1", "good2", "hang"):
            os.makedirs(os.path.join(self.tmp, sub))
            with open(os.path.join(self.tmp, sub, "f.bin"), "wb") as fh:
                fh.write(b"\0" * (1024 * 1024))
        self.hang = os.path.join(os.path.realpath(self.tmp), "hang")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_completes_and_names_the_blocking_path(self):
        import threading
        released = threading.Event()
        sc = Scanner(threads=2, top_files=5, stall_timeout=0.5)
        real = sc.listdir

        def wedging_listdir(path):
            if path == self.hang:
                released.wait(30)       # blocks until the test tears down
            return real(path)

        sc.listdir = wedging_listdir
        try:
            res = sc.scan([self.tmp])   # must return despite the wedged thread
            self.assertIn(self.hang, res.blocked)
            # the healthy siblings were still counted
            self.assertEqual(res.total_alloc[
                os.path.join(os.path.realpath(self.tmp), "good1")], 1024 * 1024)
            self.assertGreaterEqual(res.file_count, 2)
        finally:
            released.set()

    def test_late_returning_worker_does_not_end_scan_early(self):
        """A timed-out task that later completes must not be counted twice."""
        import threading
        released = threading.Event()
        sc = Scanner(threads=2, top_files=5, stall_timeout=0.3)
        real = sc.listdir

        def slow_listdir(path):
            if path == self.hang:
                released.wait(1.0)      # times out, then returns anyway
            return real(path)

        sc.listdir = slow_listdir
        res = sc.scan([self.tmp])
        released.set()
        self.assertIn(self.hang, res.blocked)
        # Every non-blocked directory must still have been visited.
        for sub in ("good1", "good2"):
            self.assertIn(os.path.join(os.path.realpath(self.tmp), sub),
                          res.total_alloc)


class TestManyStalledDirectories(unittest.TestCase):
    """More blocking directories than worker threads must still finish."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hangs = set()
        for i in range(12):
            d = os.path.join(self.tmp, f"hang{i}")
            os.makedirs(d)
            self.hangs.add(os.path.join(os.path.realpath(self.tmp), f"hang{i}"))
        for i in range(3):
            d = os.path.join(self.tmp, f"good{i}")
            os.makedirs(d)
            with open(os.path.join(d, "f.bin"), "wb") as fh:
                fh.write(b"\0" * (1024 * 1024))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finishes_with_more_hangs_than_threads(self):
        import threading
        released = threading.Event()
        sc = Scanner(threads=4, top_files=5, stall_timeout=0.3)
        real = sc.listdir

        def wedging_listdir(path):
            if path in self.hangs:
                released.wait(60)
            return real(path)

        sc.listdir = wedging_listdir
        started = time.monotonic()
        try:
            res = sc.scan([self.tmp])
            self.assertLess(time.monotonic() - started, 45,
                            "scan did not finish promptly")
            self.assertEqual(set(res.blocked), self.hangs)
            self.assertEqual(res.file_count, 3)
        finally:
            released.set()


class TestRuleOverlap(unittest.TestCase):
    """No byte may be reported twice -- an inflated reclaim figure promises
    space that does not exist."""

    def _finding(self, path, size, rule_id, generic=False, safety="safe"):
        r = rules_mod.Rule(id=rule_id, title=rule_id, safety=safety,
                           generic=generic)
        return rules_mod.Finding(r, path, size)

    def test_descendant_dropped(self):
        out = rules_mod._resolve_overlaps([
            self._finding("/a/b", 100, "outer"),
            self._finding("/a/b/c", 50, "inner"),
        ])
        self.assertEqual([f.path for f in out], ["/a/b"])

    def test_specific_rule_beats_generic_on_same_path(self):
        out = rules_mod._resolve_overlaps([
            self._finding("/a/b", 100, "catchall", generic=True),
            self._finding("/a/b", 100, "specific"),
        ])
        self.assertEqual([f.rule.id for f in out], ["specific"])

    def test_siblings_both_kept(self):
        out = rules_mod._resolve_overlaps([
            self._finding("/a/b", 100, "r1"),
            self._finding("/a/c", 50, "r2"),
        ])
        self.assertEqual(sorted(f.path for f in out), ["/a/b", "/a/c"])

    def test_prefix_is_not_containment(self):
        # /a/bcd must not be treated as living inside /a/b
        out = rules_mod._resolve_overlaps([
            self._finding("/a/b", 100, "r1"),
            self._finding("/a/bcd", 50, "r2"),
        ])
        self.assertEqual(len(out), 2)


class TestRulesFile(unittest.TestCase):
    def test_loads_and_is_well_formed(self):
        rs = rules_mod.load_rules()
        self.assertGreater(len(rs), 20)
        ids = [r.id for r in rs]
        self.assertEqual(len(ids), len(set(ids)), "duplicate rule ids")
        for r in rs:
            self.assertIn(r.safety, ("safe", "caution", "manual"), r.id)
            self.assertIn(r.action.get("kind"),
                          ("trash", "delete", "command", "manual"), r.id)
            self.assertTrue(r.why, f"{r.id} has no explanation")
            if r.action.get("kind") == "command":
                self.assertTrue(r.action.get("command"), r.id)

    def test_safe_rules_are_actually_regenerable(self):
        for r in rules_mod.load_rules():
            if r.safety == "safe" and r.action.get("kind") in ("trash", "delete"):
                self.assertTrue(
                    r.regenerates,
                    f"{r.id} is marked safe but not flagged as regenerating")

    def test_fast_roots_are_disjoint(self):
        roots = rules_mod.fast_roots(rules_mod.load_rules())
        for a in roots:
            for b in roots:
                if a != b:
                    self.assertFalse(b.startswith(a.rstrip("/") + "/"),
                                     f"{b} nested inside {a}")


if __name__ == "__main__":
    if sys.platform != "darwin":
        print("macOS only")
        sys.exit(1)
    unittest.main(verbosity=2)
