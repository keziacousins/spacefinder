# spacefinder

Find and reclaim disk space on macOS. Stdlib-only Python, no dependencies.

```sh
./sf overview          # volume, APFS container, snapshots, purgeable
./sf scan --fast       # the usual suspects, ~20s
./sf scan /            # everything, ~30s for 3.5M files
./sf rules --list      # what it knows
./sf clean --rule xcode-derived-data          # dry run
./sf clean --rule xcode-derived-data --apply  # actually do it
```

## Why it's fast

`du` and `find` call `readdir()` then `lstat()` once per file — millions of
syscalls, single-threaded. `spacefinder` uses **`getattrlistbulk(2)`**, the
macOS syscall that returns name, type, size, link count and mtime for dozens of
entries at a time. That's the same approach the fast GUI scanners use. Combined
with a thread pool (Python releases the GIL during syscalls) a full 3.5M-file
volume scan takes ~30s.

Measured on `/Applications` (49k dirs, 327k files):

| engine | time |
|---|---|
| `getattrlistbulk` | 2.1s |
| `os.scandir` + `stat` | 5.0s |

The bulk path hand-decodes a packed C struct, which is exactly the kind of code
that fails silently and reports plausible-but-wrong sizes. So it validates
itself against `os.lstat` at startup and falls back to `scandir` on any
mismatch. Force either with `--engine bulk|scandir`.

## What it measures

**Allocated bytes**, not apparent size — that's what actually frees up. Sparse
and APFS-compressed files are flagged where the two differ (`rootfs.img` might
be 10 GB logical but 5.6 GB on disk). Hard links are counted once. Totals match
`du -sk` exactly.

Only regular files carry bytes; directory inodes and symlink targets are not
counted, matching `du`'s behaviour.

## The accounting section

A tree walk structurally cannot see everything. `scan` reconciles what it found
against what the volume reports as used, and names the difference:

```
ACCOUNTING
    175.8 GB  files found by this scan
      6.9 GB  + Preboot volume (separate APFS volume)
   1000.5 MB  + Recovery volume (separate APFS volume)
           ?  + 522 unreadable directories - no Full Disk Access
  ----------
    221.6 GB  volume reports used
     37.9 GB  unexplained
```

An unexplained residual usually means one of three things:

1. **No Full Disk Access.** Mail, Messages, Photos, `~/Library/Mobile Documents`
   and ~140 other Library subdirectories return `EPERM`. Grant it to your
   terminal in System Settings → Privacy & Security → Full Disk Access.
2. **APFS local snapshots.** Time Machine snapshots pin the blocks of deleted
   files — the usual reason deleting 50 GB frees nothing. `overview` lists them.
3. **Sibling APFS volumes.** All volumes share one container pool, so Preboot
   or VM growth eats your free space and no walk of `/` will find it.

Reporting a total that is quietly tens of GB short is worse than reporting the
gap, so the gap is always shown.

## Directories that block

Not every unreadable directory fails cleanly. Without Full Disk Access, some
paths return `EPERM` immediately (fine — counted as "unreadable"), but a TCC
consent check with nothing to answer it, or an unresponsive File Provider
mount, can **block the syscall indefinitely**. No dialog appears. The thread
cannot be cancelled.

So the walker supervises its own workers. A directory that doesn't respond
within `--stall-timeout` (default 15s) is abandoned, named in the output, and a
replacement worker is started so the queue keeps draining:

```
  3 directories timed out - stopped responding, skipped:
      ~/Library/Something
```

Replacement is driven by the count of *live* workers, so a volume with more
blocking directories than threads still finishes. Worker threads are daemons —
a permanently wedged one can never stop the process from exiting.

Cloud storage roots (`~/Library/CloudStorage`, `~/Library/Mobile Documents`,
`~/Dropbox`, `~/OneDrive`) are **skipped by default**. Walking them can block on
a sync daemon, and reading them can make macOS *materialise* — download —
files that were only placeholders, which would consume disk rather than measure
it. `--include-cloud` opts in.

## rules.json

Local knowledge, codified. Each rule says where to look, what counts as a hit,
and what to do:

```json
{
  "id": "xcode-derived-data",
  "title": "Xcode DerivedData",
  "category": "developer",
  "why": "Build intermediates and indexes. Rebuilt on the next build.",
  "paths": ["~/Library/Developer/Xcode/DerivedData/*"],
  "match": {"kind": "dir", "min_size_mb": 50},
  "safety": "safe",
  "regenerates": true,
  "action": {"kind": "trash"},
  "note": "Quit Xcode first. First build afterwards is slow."
}
```

| field | meaning |
|---|---|
| `paths` | glob patterns, `~` expanded |
| `find` | `{"names": ["node_modules"], "under": ["~"]}` — match a directory name anywhere |
| `match` | `kind` (dir/file), `min_size_mb`, `older_than_days`, `name_glob` |
| `safety` | `safe` (regenerated automatically) / `caution` (you lose a cache) / `manual` (needs judgement) |
| `action` | `trash` \| `delete` \| `command` (a shell one-liner) \| `manual` |
| `generic` | catch-all rule; loses to a specific rule matching the same path |
| `regenerates` | asserted by tests for anything marked `safe` |

Rules overlap on purpose — `~/Library/Caches/Homebrew` matches both the specific
`homebrew-cache` rule and the catch-all `user-caches` rule. Overlaps are
resolved before totalling (specific beats generic; a nested hit is dropped
because it's already inside its ancestor's total) so the reclaim figure never
double-counts.

To add knowledge, add a rule. `--rules other.json` uses a different file.

## Safety

- `clean` is **dry-run by default**; `--apply` is required to touch anything,
  and prompts unless `--yes`.
- `--safety safe` is the default and only acts on regenerable data. Raise it
  deliberately with `--safety caution`.
- The `trash` action moves things to `~/.Trash` — reversible. Space is not
  returned until you empty it, and the tool says so rather than claiming a
  reclaim that hasn't happened.
- `manual` rules are never executed, only reported.

## Options

```
--engine bulk|scandir   force an enumeration engine
--threads N             default: 2x cores, capped at 16
--cross-device          follow into other mounted volumes (off by default)
--stall-timeout SECS    abandon a directory that blocks this long (default 15)
--include-cloud         also walk iCloud/Google Drive/Dropbox mounts
--min-size MB           ignore findings below this
--json out.json         full machine-readable results
--all                   show every finding, not the top 12
```

`/net`, `/home` (autofs — these hang on a stale mount), `/dev`, and
`/System/Volumes/Data` are always skipped. That last one matters: it's
firmlinked to `/`, so walking both would count everything twice.

## Tests

```sh
venv/bin/python tests.py
```

Covers engine agreement across ~500 real directory entries, rollups against
`du`, hard-link dedup, overlap resolution, and rules.json well-formedness.
