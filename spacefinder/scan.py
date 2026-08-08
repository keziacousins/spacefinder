"""Threaded filesystem walker with directory rollups."""

from __future__ import annotations

import heapq
import os
import queue
import threading
import time
from dataclasses import dataclass, field

from . import fastwalk
from .fastwalk import DT_DIR, DT_FILE

# Paths that hang, recurse forever, or double-count.  On macOS `/` and
# /System/Volumes/Data are firmlinked: walking both counts everything twice.
# /net and /home are autofs and will block indefinitely on a stale mount.
ALWAYS_SKIP = {
    "/System/Volumes/Data",
    "/System/Volumes/Preboot",
    "/System/Volumes/VM",
    "/System/Volumes/Update",
    "/System/Volumes/xarts",
    "/System/Volumes/iSCPreboot",
    "/System/Volumes/Hardware",
    "/net",
    "/home",
    "/dev",
    "/.vol",
}

# Network-backed File Provider mounts (iCloud Drive, Google Drive, Dropbox,
# OneDrive).  Skipped by default for two reasons: walking them can block
# indefinitely waiting on a sync daemon, and merely reading them can cause
# macOS to materialise -- i.e. download -- files that were only placeholders,
# which would *use* disk space rather than measure it.  --include-cloud opts in.
CLOUD_ROOTS = [
    "~/Library/CloudStorage",
    "~/Library/Mobile Documents",
    "~/Dropbox",
    "~/OneDrive",
]

# Locations behind TCC.  Without Full Disk Access these do not fail cleanly --
# they block on a consent check, and macOS attributes each outstanding request
# to whichever application launched the scan, not to spacefinder.  An
# interactive terminal can sometimes surface that dialog, but a scan generates
# far more requests than anyone will answer, and an editor-embedded shell or a
# cron job cannot ask at all.  The recorded denials outlive the scan.
#
# So when we know we lack access these are skipped rather than probed.  There
# is no version of walking them that succeeds without FDA: it either blocks or
# spends someone else's permission grant.  The bytes are attributed in the
# accounting section instead, which keeps the residual named.
TCC_PROTECTED = [
    "~/Library/Mail",
    "~/Library/Messages",
    "~/Library/Safari",
    "~/Library/Cookies",
    "~/Library/Calendars",
    "~/Library/Reminders",
    "~/Library/Accounts",
    "~/Library/AddressBook",
    "~/Library/HomeKit",
    "~/Library/IdentityServices",
    "~/Library/Metadata/CoreSpotlight",
    "~/Library/Suggestions",
    "~/Library/Trial",
    "~/Library/Autosave Information",
    "~/Library/Application Support/com.apple.TCC",
    "~/Library/Application Support/AddressBook",
    "~/Library/Application Support/CallHistoryDB",
    "~/Library/Application Support/CallHistoryTransactions",
    "~/Library/Containers/com.apple.mail",
    "~/Library/Group Containers/group.com.apple.notes",
]


@dataclass
class BigFile:
    path: str
    alloc: int
    logical: int
    mtime: float


@dataclass
class ScanResult:
    roots: list[str] = field(default_factory=list)
    self_alloc: dict[str, int] = field(default_factory=dict)
    self_logical: dict[str, int] = field(default_factory=dict)
    total_alloc: dict[str, int] = field(default_factory=dict)
    total_logical: dict[str, int] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)
    big_files: list[BigFile] = field(default_factory=list)
    file_count: int = 0
    dir_count: int = 0
    denied: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    tcc_skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dedup_saved: int = 0
    elapsed: float = 0.0
    engine: str = ""

    @property
    def grand_total(self) -> int:
        return sum(self.total_alloc.get(r, 0) for r in self.roots)

    def top_dirs(self, n: int = 25, max_depth: int | None = None):
        """Largest directories by rolled-up allocated bytes.

        ``max_depth`` limits only what is *reported*; the scan itself is always
        complete, so totals never silently undercount.
        """
        items = []
        for path, size in self.total_alloc.items():
            if max_depth is not None:
                root = next((r for r in self.roots if path.startswith(r)), None)
                if root is None:
                    continue
                rel = path[len(root):].strip("/")
                depth = 0 if not rel else rel.count("/") + 1
                if depth > max_depth:
                    continue
            items.append((size, path))
        items.sort(reverse=True)
        return items[:n]


class Scanner:
    def __init__(self, threads=None, engine=None, skip=None, top_files=40,
                 one_filesystem=True, progress=None, stall_timeout=15.0,
                 include_cloud=False, has_fda=None, blocked_cap=32):
        self.engine_name, self.listdir = fastwalk.get_engine(engine)
        self.threads = threads or min(16, (os.cpu_count() or 4) * 2)
        self.skip = set(ALWAYS_SKIP) | {os.path.realpath(os.path.expanduser(p))
                                        for p in (skip or ())}
        if not include_cloud:
            self.skip |= {os.path.expanduser(p) for p in CLOUD_ROOTS}
        self.tcc_skipped: list[str] = []
        if has_fda is not True:
            for p in TCC_PROTECTED:
                p = os.path.expanduser(p)
                self.skip.add(p)
                if os.path.isdir(p):
                    self.tcc_skipped.append(p)
        self.top_files_n = top_files
        self.one_filesystem = one_filesystem
        self.progress = progress
        self.stall_timeout = stall_timeout
        self.blocked_cap = blocked_cap

        self._lock = threading.Lock()
        self._heap: list[tuple[int, str, int, float]] = []
        self._seen_links: set[tuple[int, int]] = set()
        self._root_devs: set[int] = set()
        self._inflight: dict[int, tuple[str, float, int]] = {}
        self._wedged: set[int] = set()
        self._submitted = 0
        self._completed = 0
        self._task_seq = 0
        self._next_wid = 0

    def scan(self, roots: list[str]) -> ScanResult:
        started = time.monotonic()
        res = ScanResult(engine=self.engine_name,
                         tcc_skipped=list(self.tcc_skipped))
        q: queue.LifoQueue = queue.LifoQueue()

        seeded: list[str] = []
        for r in roots:
            r = os.path.realpath(os.path.expanduser(r))
            if not os.path.isdir(r):
                continue
            # Drop roots nested inside an already-seeded root: they would be
            # walked twice and double-counted in the grand total.
            if any(r == s or r.startswith(s.rstrip("/") + "/") for s in seeded):
                continue
            seeded.append(r)
            try:
                self._root_devs.add(os.stat(r).st_dev)
            except OSError:
                pass
            self._submitted += 1
            q.put((r, 0))
        res.roots = seeded

        stop = threading.Event()
        threads = []
        for _ in range(self.threads):
            threads.append(self._spawn(q, res, stop))

        try:
            self._supervise(q, res, stop, threads)
        finally:
            stop.set()
            for w in threads:
                w.join(timeout=0.5)

        self._rollup(res)
        res.big_files = [BigFile(p, a, lg, m)
                         for a, p, lg, m in sorted(self._heap, reverse=True)]
        res.elapsed = time.monotonic() - started
        return res

    def _spawn(self, q, res, stop):
        with self._lock:
            wid = self._next_wid
            self._next_wid += 1
        t = threading.Thread(target=self._worker, args=(q, res, stop, wid),
                             daemon=True, name=f"scan-{wid}")
        t.start()
        return t

    def _supervise(self, q, res, stop, threads):
        """Drive the scan to completion, surviving wedged workers.

        A directory access can block indefinitely rather than returning EPERM
        -- a TCC consent check with no one to answer it, or an unresponsive
        network/File Provider mount.  The blocked thread cannot be cancelled,
        so instead we time it out, account for its task, name the path, and
        start a replacement worker.  Threads are daemons, so a permanently
        wedged one never stops the process exiting.
        """
        spawn_cap = max(self.threads * 8, 128)
        while True:
            time.sleep(0.05)
            now = time.monotonic()
            with self._lock:
                if self._completed >= self._submitted and not self._inflight:
                    return
                stuck = [(wid, path)
                         for wid, (path, t0, _tid) in self._inflight.items()
                         if now - t0 > self.stall_timeout]
                for wid, path in stuck:
                    del self._inflight[wid]
                    self._wedged.add(wid)
                    self._completed += 1      # the wedged thread will not
                    res.blocked.append(path)  # count it; see _worker
                live = len(threads) - len(self._wedged)
                # Circuit breaker.  One blocking directory is a bad mount;
                # dozens means something systemic -- almost always a consent
                # check nobody is answering.  Spawning replacements into that
                # generates more outstanding requests, and macOS records them
                # against the application that launched us.  Stop instead.
                if len(res.blocked) >= self.blocked_cap:
                    res.errors.append(
                        f"stopped after {len(res.blocked)} directories blocked "
                        f"-- looks like missing Full Disk Access rather than "
                        f"one unresponsive mount")
                    return

            # Replace wedged threads so the queue keeps draining.  Counting
            # live workers (rather than total spawned) means a volume with
            # many blocking directories still finishes.
            while live < self.threads and len(threads) < spawn_cap:
                threads.append(self._spawn(q, res, stop))
                live += 1

            if live == 0 and len(threads) >= spawn_cap:
                with self._lock:
                    res.errors.append(
                        f"aborted: {len(self._wedged)} workers blocked on "
                        f"unresponsive directories")
                return

    # -- workers ----------------------------------------------------------

    def _worker(self, q, res, stop, wid):
        local: list[tuple[int, str, int, float]] = []
        while not stop.is_set():
            try:
                path, depth = q.get(timeout=0.05)
            except queue.Empty:
                if local:
                    self._merge_heap(local)
                    local = []
                continue
            with self._lock:
                self._task_seq += 1
                tid = self._task_seq
                self._inflight[wid] = (path, time.monotonic(), tid)
            try:
                self._visit(path, depth, q, res, local)
            except Exception as exc:            # never let a worker die silently
                with self._lock:
                    res.errors.append(f"{path}: {exc!r}")
            finally:
                with self._lock:
                    cur = self._inflight.get(wid)
                    if cur is not None and cur[2] == tid:
                        del self._inflight[wid]
                        self._completed += 1
                    else:
                        # The supervisor already timed this task out and
                        # counted it; counting again would end the scan early.
                        # We are responsive again, so rejoin the live pool.
                        self._wedged.discard(wid)
            if len(local) > self.top_files_n * 4:
                self._merge_heap(local)
                local = []
        if local:
            self._merge_heap(local)

    def _visit(self, path, depth, q, res, local):
        try:
            entries = self.listdir(path)
        except PermissionError:
            with self._lock:
                res.denied.append(path)
            return
        except OSError as exc:
            with self._lock:
                res.errors.append(f"{path}: {exc.strerror}")
            return

        self_alloc = 0
        self_logical = 0
        nfiles = 0
        subdirs = []
        top_n = self.top_files_n

        for e in entries:
            if e.kind == DT_DIR:
                if self.one_filesystem and e.dev and e.dev not in self._root_devs:
                    continue
                child = path + "/" + e.name if path != "/" else "/" + e.name
                if child in self.skip:
                    continue
                subdirs.append(child)
            elif e.kind == DT_FILE:
                alloc = e.alloc
                if e.nlink > 1:
                    key = (e.dev, e.ino)
                    with self._lock:
                        if key in self._seen_links:
                            res.dedup_saved += alloc
                            alloc = 0
                        else:
                            self._seen_links.add(key)
                self_alloc += alloc
                self_logical += e.logical
                nfiles += 1
                if alloc:
                    fp = path + "/" + e.name if path != "/" else "/" + e.name
                    item = (alloc, fp, e.logical, e.mtime)
                    if len(local) < top_n:
                        heapq.heappush(local, item)
                    elif item[0] > local[0][0]:
                        heapq.heapreplace(local, item)

        with self._lock:
            res.self_alloc[path] = self_alloc
            res.self_logical[path] = self_logical
            res.children[path] = subdirs
            res.file_count += nfiles
            res.dir_count += 1
            # Counted here, before the puts, so the supervisor can never see
            # completed == submitted while children are still being enqueued.
            self._submitted += len(subdirs)
            if self.progress and res.dir_count % 5000 == 0:
                self.progress(res.dir_count, res.file_count)

        for child in subdirs:
            q.put((child, depth + 1))

    def _merge_heap(self, local):
        with self._lock:
            for item in local:
                if len(self._heap) < self.top_files_n:
                    heapq.heappush(self._heap, item)
                elif item[0] > self._heap[0][0]:
                    heapq.heapreplace(self._heap, item)

    # -- rollup -----------------------------------------------------------

    @staticmethod
    def _rollup(res: ScanResult):
        """Iterative post-order sum of self sizes up the tree.

        Iterative rather than recursive: deep trees (node_modules, Xcode
        archives) blow the Python recursion limit.
        """
        alloc, logical = res.total_alloc, res.total_logical
        for root in res.roots:
            stack = [(root, False)]
            while stack:
                node, expanded = stack.pop()
                if expanded:
                    a = res.self_alloc.get(node, 0)
                    lg = res.self_logical.get(node, 0)
                    for c in res.children.get(node, ()):
                        a += alloc.get(c, 0)
                        lg += logical.get(c, 0)
                    alloc[node] = a
                    logical[node] = lg
                else:
                    stack.append((node, True))
                    for c in res.children.get(node, ()):
                        stack.append((c, False))
