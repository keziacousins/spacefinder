"""Terminal output."""

from __future__ import annotations

import os
import sys
import time

from .sysinfo import human

_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

if _TTY:
    BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
    RED, YEL, GRN, CYN = "\033[31m", "\033[33m", "\033[32m", "\033[36m"
else:
    BOLD = DIM = RESET = RED = YEL = GRN = CYN = ""

SAFETY_COLOUR = {"safe": GRN, "caution": YEL, "manual": CYN}


def _c(s, colour):
    return f"{colour}{s}{RESET}" if _TTY else str(s)


def bar(frac: float, width: int = 24) -> str:
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


def print_overview(info):
    print(f"\n{_c('VOLUME', BOLD)}  {info.mount}")
    used_frac = info.used / info.total if info.total else 0
    colour = RED if used_frac > 0.9 else YEL if used_frac > 0.75 else GRN
    print(f"  {_c(bar(used_frac), colour)}  {used_frac*100:.1f}% used")
    print(f"  {human(info.used)} used / {human(info.total)}   "
          f"{_c(human(info.free) + ' free', colour)}")

    if info.container_size:
        print(f"\n{_c('APFS CONTAINER', BOLD)} (all volumes share this pool)")
        for dev, role, consumed in info.volumes:
            print(f"  {dev:<10} {role:<10} {human(consumed):>10}")
        print(f"  {'':<10} {'unallocated':<10} {human(info.container_free):>10}")

    if info.purgeable > 0:
        print(f"\n{_c('PURGEABLE', BOLD)}  {human(info.purgeable)}")
        print(f"  {DIM}Held by macOS and released under pressure; "
              f"not visible to a file scan.{RESET}")

    if info.snapshots:
        print(f"\n{_c('LOCAL SNAPSHOTS', BOLD)}  {len(info.snapshots)}")
        for s in info.snapshots[:8]:
            print(f"  {s}")
        print(f"  {_c('These pin the blocks of deleted files.', YEL)}")
        print(f"  {DIM}Remove with: tmutil deletelocalsnapshots /{RESET}")
    else:
        print(f"\n{_c('LOCAL SNAPSHOTS', BOLD)}  none "
              f"{DIM}(so deleted files really are freeing space){RESET}")


def print_scan(res, top=25, max_depth=None, show_files=20):
    print(f"\n{_c('SCAN', BOLD)}  {', '.join(res.roots)}")
    print(f"  {res.dir_count:,} dirs, {res.file_count:,} files in "
          f"{res.elapsed:.1f}s  {DIM}(engine: {res.engine}){RESET}")
    print(f"  total {_c(human(res.grand_total), BOLD)}")
    if res.dedup_saved:
        print(f"  {DIM}{human(res.dedup_saved)} of hard links counted once"
              f"{RESET}")
    if res.denied:
        print(f"  {_c(f'{len(res.denied)} directories unreadable', YEL)} "
              f"{DIM}- totals are low by that amount{RESET}")
    if res.blocked:
        print(f"  {_c(f'{len(res.blocked)} directories timed out', RED)} "
              f"{DIM}- stopped responding, skipped:{RESET}")
        for p in res.blocked[:6]:
            print(f"      {_shorten(p)}")
        if len(res.blocked) > 6:
            print(f"      {DIM}... and {len(res.blocked)-6} more{RESET}")

    print(f"\n{_c('LARGEST DIRECTORIES', BOLD)}")
    total = res.grand_total or 1
    for size, path in res.top_dirs(top, max_depth=max_depth):
        frac = size / total
        print(f"  {human(size):>9}  {DIM}{bar(frac, 12)}{RESET} "
              f"{frac*100:4.1f}%  {_shorten(path)}")

    if show_files and res.big_files:
        print(f"\n{_c('LARGEST FILES', BOLD)}")
        for f in res.big_files[:show_files]:
            extra = ""
            if f.logical > f.alloc * 1.5 and f.alloc:
                extra = f" {DIM}(sparse/compressed: {human(f.logical)} logical){RESET}"
            print(f"  {human(f.alloc):>9}  {_shorten(f.path)}{extra}")


def print_accounting(res, info, has_fda: bool):
    """Reconcile what the walk found against what the volume says is used.

    A tree scan structurally cannot see snapshots, purgeable space, sibling
    APFS volumes, or anything behind a TCC prompt.  Printing the residual --
    rather than quietly reporting a total that is tens of GB short -- is the
    difference between a useful answer and a misleading one.
    """
    print(f"\n{_c('ACCOUNTING', BOLD)}")
    scanned = res.grand_total
    print(f"  {human(scanned):>10}  files found by this scan")

    unwalkable = 0
    roots = set(res.roots)
    walked_system = any(r == "/" for r in roots)
    for dev, role, consumed in info.volumes:
        # Preboot/Recovery are separate APFS volumes sharing the container;
        # nothing under / reaches them.
        if role in ("Preboot", "Recovery", "xarts", "iSCPreboot", "Hardware"):
            unwalkable += consumed
            print(f"  {human(consumed):>10}  + {role} volume "
                  f"{DIM}(separate APFS volume){RESET}")
        elif role == "System" and not walked_system:
            unwalkable += consumed
            print(f"  {human(consumed):>10}  + System volume "
                  f"{DIM}(not scanned; run against /){RESET}")

    if info.purgeable:
        unwalkable += info.purgeable
        print(f"  {human(info.purgeable):>10}  + purgeable "
              f"{DIM}(macOS releases under pressure){RESET}")

    snap_note = ""
    if info.snapshots:
        snap_note = f" ({len(info.snapshots)} local snapshots)"
        print(f"  {'?':>10}  + APFS snapshots{snap_note} "
              f"{DIM}pin deleted blocks{RESET}")

    if res.denied:
        print(f"  {'?':>10}  + {len(res.denied)} unreadable directories"
              + (f" {_c('- no Full Disk Access', YEL)}" if not has_fda else ""))

    residual = info.used - scanned - unwalkable
    print(f"  {'-' * 10}")
    print(f"  {human(info.used):>10}  volume reports used")
    colour = RED if residual > 20 * 2**30 else YEL if residual > 5 * 2**30 else GRN
    print(f"  {_c(f'{human(residual):>10}', colour)}  unexplained")

    if residual > 5 * 2**30 and not has_fda:
        print(f"\n  {_c('Most of this is probably behind Full Disk Access.', YEL)}")
        print(f"  {DIM}System Settings > Privacy & Security > Full Disk "
              f"Access > add your terminal, then re-run.{RESET}")
    elif residual > 20 * 2**30:
        print(f"\n  {DIM}Large unexplained residual: check 'spacefinder "
              f"overview' for snapshots, and other volumes in the "
              f"container.{RESET}")


def print_findings(findings, show_all=False):
    if not findings:
        print(f"\n{_c('No rules matched.', DIM)}")
        return

    print(f"\n{_c('RECLAIMABLE (by rule)', BOLD)}")
    by_safety = {"safe": [], "caution": [], "manual": []}
    for f in findings:
        by_safety.setdefault(f.rule.safety, []).append(f)

    for safety in ("safe", "caution", "manual"):
        group = by_safety.get(safety) or []
        if not group:
            continue
        total = sum(f.size for f in group)
        label = {
            "safe": "SAFE - regenerated automatically",
            "caution": "CAUTION - you lose a cache you may want",
            "manual": "MANUAL - needs your judgement",
        }[safety]
        print(f"\n  {_c(label, SAFETY_COLOUR[safety])}  "
              f"{_c(human(total), BOLD)} total")
        shown = group if show_all else group[:12]
        for f in shown:
            age = f" {DIM}{f.age_days:.0f}d old{RESET}" if f.mtime else ""
            print(f"    {human(f.size):>9}  {_shorten(f.path)}{age}")
            print(f"               {DIM}[{f.rule.id}] {f.rule.why}{RESET}")
            act = f.rule.action
            if act.get("kind") == "command":
                print(f"               {DIM}$ {act['command']}{RESET}")
            if f.rule.note:
                print(f"               {DIM}! {f.rule.note}{RESET}")
        if len(group) > len(shown):
            print(f"    {DIM}... {len(group)-len(shown)} more "
                  f"(--all to show){RESET}")

    safe_total = sum(f.size for f in by_safety.get("safe", []))
    print(f"\n  {_c('Safe to reclaim now: ' + human(safe_total), GRN)}")


def _shorten(path: str, width: int = 76) -> str:
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    if len(path) <= width:
        return path
    head, tail = path[:width // 3], path[-(width * 2 // 3):]
    return f"{head}...{tail}"


def progress_printer():
    state = {"t": time.monotonic()}

    def cb(dirs, files):
        now = time.monotonic()
        if now - state["t"] < 0.2:
            return
        state["t"] = now
        if sys.stderr.isatty():
            print(f"\r  scanning... {dirs:,} dirs  {files:,} files",
                  end="", file=sys.stderr, flush=True)

    return cb


def clear_progress():
    if sys.stderr.isatty():
        print("\r" + " " * 60 + "\r", end="", file=sys.stderr, flush=True)
