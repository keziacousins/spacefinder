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

from spacefinder import clean, fastwalk, report, rules as rules_mod
from spacefinder import scan as scan_mod
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


class TestPathGuard(unittest.TestCase):
    """A rules file drives deletion, so the action layer is the last line."""

    def test_protected_locations_refused(self):
        home = os.path.expanduser("~")
        for p in ("/", home, f"{home}/Library", f"{home}/Documents",
                  f"{home}/Downloads", "/private/var/folders"):
            self.assertIsNotNone(clean.check_path(p), f"{p} was allowed")

    def test_outside_allowed_roots_refused(self):
        for p in ("/etc/passwd", "/System/Library", "/Applications/Safari.app"):
            self.assertIsNotNone(clean.check_path(p), f"{p} was allowed")

    def test_legitimate_targets_allowed(self):
        home = os.path.expanduser("~")
        for p in (f"{home}/Library/Caches/Homebrew", f"{home}/Downloads/x.dmg",
                  "/private/var/folders/ab/cd/C/thing"):
            self.assertIsNone(clean.check_path(p), f"{p} was refused")

    def test_traversal_cannot_escape(self):
        self.assertIsNotNone(
            clean.check_path(os.path.expanduser("~/Library/../../../etc")))

    def test_actions_enforce_the_guard(self):
        # Even in dry-run: a refusal must be visible before --apply, not after.
        out = clean.trash("/", dry_run=True)
        self.assertFalse(out.ok, "trash accepted /")
        self.assertIn("refused", out.detail)

    def test_credentials_refused(self):
        """Depth alone permitted these: they are named outright instead.

        A rules file that reached ~/.ssh or the login keychain would move
        credentials to the Trash at the default safety level.
        """
        home = os.path.expanduser("~")
        for p in (f"{home}/.ssh", f"{home}/.ssh/id_ed25519",
                  f"{home}/.aws/credentials", f"{home}/.gnupg/secring.gpg",
                  f"{home}/Library/Keychains",
                  f"{home}/Library/Keychains/login.keychain-db",
                  f"{home}/.config/gh/hosts.yml",
                  f"{home}/Library/Cookies/Cookies.binarycookies"):
            self.assertIsNotNone(clean.check_path(p), f"{p} was allowed")

    def test_hostile_rule_cannot_reach_credentials(self):
        """End to end: the guard holds at the action, not just in isolation."""
        r = rules_mod.Rule(id="hostile", title="hostile", safety="safe",
                           action={"kind": "trash"})
        f = rules_mod.Finding(
            r, os.path.expanduser("~/Library/Keychains/login.keychain-db"), 1)
        out = clean.apply_finding(f, dry_run=False)
        self.assertFalse(out.ok)
        self.assertIn("sensitive", out.detail)


class TestNoCommandExecution(unittest.TestCase):
    """spacefinder reports commands. It never runs them."""

    def _finding(self, cmd):
        r = rules_mod.Rule(id="t", title="t",
                           action={"kind": "command", "command": cmd})
        return rules_mod.Finding(r, "/tmp/whatever", 0)

    def test_command_is_only_advice(self):
        out = clean.apply_finding(self._finding("echo hi"), dry_run=False)
        self.assertEqual(out.action, "advice")

    def test_command_with_apply_does_not_execute(self):
        marker = os.path.join(tempfile.mkdtemp(), "executed")
        f = self._finding(f"touch {marker}")
        clean.apply_finding(f, dry_run=False)
        self.assertFalse(os.path.exists(marker),
                         "a rule's shell command was executed")

    def test_no_execution_machinery_remains(self):
        # Guards against a well-meaning reintroduction.
        for gone in ("run_command", "delete"):
            self.assertFalse(hasattr(clean, gone),
                             f"clean.{gone} is back")
        self.assertNotIn("subprocess", dir(clean))


class TestTrashLog(unittest.TestCase):
    """The undo story is "move it back", which needs both paths recorded."""

    def test_records_source_and_destination(self):
        d = tempfile.mkdtemp()
        log = clean.TrashLog(os.path.join(d, "log"))
        log.record("/a/b", "/c/d")
        written = log.flush()
        self.assertIsNotNone(written)
        with open(written) as fh:
            line = fh.read().strip()
        self.assertIn("/a/b", line)
        self.assertIn("/c/d", line)

    def test_written_private(self):
        d = tempfile.mkdtemp()
        log = clean.TrashLog(os.path.join(d, "log"))
        log.record("/a/b", "/c/d")
        written = log.flush()
        self.assertEqual(os.stat(written).st_mode & 0o777, 0o600)


class TestTerminalEscaping(unittest.TestCase):
    """This report is read before someone decides to delete something."""

    def test_control_characters_escaped(self):
        hostile = "/tmp/a\x1b[2K\x1b[1Gnot-what-it-says"
        out = report._shorten(hostile)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\r", out)

    def test_escaping_happens_before_truncation(self):
        # Otherwise an escaped name can exceed the width it was measured at.
        out = report._shorten("/tmp/" + "\x1b" * 60, width=40)
        self.assertLessEqual(len(out), 40)

    def test_safe_text_handles_all_c0_and_del(self):
        raw = "".join(chr(c) for c in range(0x20)) + "\x7f"
        self.assertNotIn("\x1b", report.safe_text(raw))
        self.assertTrue(all(ord(c) >= 0x20 for c in report.safe_text(raw)))


class TestRootSelection(unittest.TestCase):
    """--fast must not quietly mean 'walk the whole home directory'."""

    def test_fast_roots_do_not_collapse_to_home(self):
        rs = rules_mod.load_rules()
        home = os.path.realpath(os.path.expanduser("~"))
        self.assertNotIn(home, rules_mod.fast_roots(rs))

    def test_fast_roots_are_specific(self):
        rs = rules_mod.load_rules()
        self.assertGreater(len(rules_mod.fast_roots(rs)), 5)

    def test_scan_roots_cover_name_based_rules(self):
        rs = rules_mod.load_rules()
        roots = rules_mod.scan_roots(rs)
        home = os.path.realpath(os.path.expanduser("~"))
        self.assertTrue(any(home == r or home.startswith(r.rstrip("/") + "/")
                            for r in roots),
                        "find.names rules would silently match nothing")

    def test_deep_rules_are_named(self):
        ids = {r.id for r in rules_mod.deep_rules(rules_mod.load_rules())}
        self.assertIn("node-modules", ids)


class TestUniqueDestination(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_never_returns_an_existing_path(self):
        """shutil.move onto an existing dir nests instead of failing."""
        base = os.path.join(self.tmp, "thing")
        os.makedirs(base)
        seen = set()
        for _ in range(3):
            dest = clean._unique(base)
            self.assertFalse(os.path.lexists(dest), f"{dest} already exists")
            self.assertNotIn(dest, seen)
            seen.add(dest)
            os.makedirs(dest)


class TestTCCContainment(unittest.TestCase):
    """Probing protected paths without FDA spends the host app's grant."""

    def test_protected_paths_skipped_without_fda(self):
        sc = Scanner(top_files=5, has_fda=False)
        for p in scan_mod.TCC_PROTECTED:
            self.assertIn(os.path.expanduser(p), sc.skip, p)

    def test_protected_paths_walked_with_fda(self):
        sc = Scanner(top_files=5, has_fda=True)
        for p in scan_mod.TCC_PROTECTED:
            self.assertNotIn(os.path.expanduser(p), sc.skip, p)

    def test_unknown_fda_is_treated_as_absent(self):
        # None means the probe could not tell. Erring towards walking would
        # risk the grant; erring towards skipping only costs completeness.
        sc = Scanner(top_files=5, has_fda=None)
        self.assertIn(os.path.expanduser(scan_mod.TCC_PROTECTED[0]), sc.skip)

    def test_skipped_paths_reported(self):
        sc = Scanner(top_files=5, has_fda=False)
        res = sc.scan([tempfile.mkdtemp()])
        self.assertEqual(res.tcc_skipped, sc.tcc_skipped)


class TestBlockedCircuitBreaker(unittest.TestCase):
    """Dozens of blocked directories means systemic denial, not a bad mount."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hangs = set()
        for i in range(20):
            d = os.path.join(self.tmp, f"hang{i}")
            os.makedirs(d)
            self.hangs.add(os.path.join(os.path.realpath(self.tmp), f"hang{i}"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_aborts_instead_of_spawning_more_probes(self):
        import threading
        released = threading.Event()
        sc = Scanner(threads=2, top_files=5, stall_timeout=0.2,
                     blocked_cap=5, has_fda=True)
        real = sc.listdir

        def wedging_listdir(path):
            if path in self.hangs:
                released.wait(30)
            return real(path)

        sc.listdir = wedging_listdir
        try:
            res = sc.scan([self.tmp])
            self.assertGreaterEqual(len(res.blocked), 5)
            self.assertLess(len(res.blocked), len(self.hangs),
                            "kept probing past the cap")
            self.assertTrue(any("Full Disk Access" in e for e in res.errors),
                            f"no explanation given: {res.errors}")
        finally:
            released.set()


class TestRulesSafety(unittest.TestCase):
    def test_no_rule_destroys_docker_volumes(self):
        for r in rules_mod.load_rules():
            cmd = r.action.get("command", "")
            self.assertNotIn("--volumes", cmd,
                             f"{r.id} would destroy named Docker volumes")

    def test_commands_are_unique_across_rules(self):
        # Two rules sharing a command means it runs twice in one clean.
        cmds = [r.action["command"] for r in rules_mod.load_rules()
                if r.action.get("kind") == "command"]
        self.assertEqual(len(cmds), len(set(cmds)), "duplicate command")

    def test_every_rule_can_actually_match(self):
        # A rule with no paths and no find.names never produces a finding,
        # so it silently advertises a capability that does not exist.
        for r in rules_mod.load_rules():
            self.assertTrue(r.paths or r.find_names,
                            f"{r.id} can never match anything")


if __name__ == "__main__":
    if sys.platform != "darwin":
        print("macOS only")
        sys.exit(1)
    unittest.main(verbosity=2)
