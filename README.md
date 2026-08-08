# spacefinder

spacefinder shows you which files use the disk space on your Mac. It can also
remove some of those files. spacefinder uses only the Python standard library.
It has no other dependencies.

## Commands

```sh
./sf overview          # show the volume, the APFS container and the snapshots
./sf scan --fast       # examine only the locations that the rules name
./sf scan /            # examine the full disk
./sf rules --list      # show all of the rules
./sf clean --rule xcode-derived-data           # show what the command removes
./sf clean --rule xcode-derived-data --apply   # remove the files
```

The `clean` command shows the files, but it does not remove them. To remove the
files, add `--apply`.

## Full Disk Access

macOS keeps some folders behind a privacy control. Mail, Messages and several
folders in `~/Library` are examples. A program cannot read these folders
without Full Disk Access.

Give Full Disk Access to the application that starts spacefinder. This
application is your terminal or your editor. macOS records the permission
against that application; it does not record the permission against
spacefinder. If the permission is absent, spacefinder shows you the name of the
correct application.

To give the permission, do these steps:

1. Open System Settings.
2. Select Privacy & Security.
3. Select Full Disk Access.
4. Add the application.

Without the permission, spacefinder does not examine the protected folders.
This behaviour is deliberate. A protected folder does not refuse access
immediately. The system call stops and waits for an answer to a permission
question. One scan can make many of these requests. macOS can record the
requests as refusals. The application then loses a permission that it had
before. spacefinder therefore skips these folders and shows you how many it
skipped.

## Speed

spacefinder uses the `getattrlistbulk(2)` system call. This call gives the
name, the type, the size, the link count and the modification time for many
directory entries together. The `du` and `find` tools use `readdir()` and then
one `lstat()` call for each file. That method makes millions of system calls,
and it uses one thread only. spacefinder uses a thread pool, because Python
releases the GIL during a system call.

| engine | time |
|---|---|
| `getattrlistbulk` | 2.0 s |
| `os.scandir` and `stat` | 4.6 s |

These times are for `/Applications`, which holds 48,845 directories and 327,273
files. The test machine is an Apple M2 with 8 cores. The operating system is
macOS 15.7.9.

A scan of the full disk finds 2.6 million files in approximately 21 seconds.
The first scan after you start the computer is slower, because the file
metadata is not in the cache. That scan takes approximately 46 seconds.

The bulk method decodes a packed C structure. Code of this type can fail and
give sizes that look correct but are wrong. spacefinder therefore compares its
result with `os.lstat` at start. If the two results disagree, spacefinder uses
`os.scandir` instead. To select an engine yourself, use `--engine bulk` or
`--engine scandir`.

### The --fast option

The `--fast` option examines only the directories that the rules name. It does
not examine the full home directory.

Some rules search for a directory name instead of a path. The `node-modules`
rule is an example. To answer these rules, spacefinder must examine the full
home directory, so `--fast` does not evaluate them. It shows you which rules it
did not evaluate.

`--fast` is not much quicker than a scan of your home directory. The named
directories hold most of the files in the home directory. Use `--fast` when you
want the rule results without a scan of the full disk.

## What spacefinder measures

spacefinder measures the allocated bytes. It does not measure the apparent
size. The allocated bytes become available when you remove the file.

Some files use less space than their apparent size. Sparse files and
APFS-compressed files are examples. spacefinder shows both numbers when the two
numbers are different.

spacefinder counts a hard link one time only. The totals agree with `du -sk`.

Only regular files hold bytes. spacefinder does not count directory inodes or
symlink targets. The `du` tool uses the same rule.

## The accounting section

A scan of the files cannot find all of the used space. The `scan` command
therefore compares its total with the total that the volume reports. It then
shows the difference.

```
ACCOUNTING
    175.8 GB  files found by this scan
      6.9 GB  + Preboot volume (separate APFS volume)
   1000.5 MB  + Recovery volume (separate APFS volume)
           ?  + 19 folders behind Full Disk Access (skipped, not probed)
           ?  + 522 unreadable directories - no Full Disk Access
  ----------
    221.6 GB  volume reports used
     37.9 GB  unexplained
```

A large difference usually has one of these three causes:

1. **spacefinder does not have Full Disk Access.** Mail, Messages, Photos and
   many other folders in `~/Library` are not available. Read the Full Disk
   Access section above.
2. **APFS local snapshots hold the blocks.** Time Machine keeps local
   snapshots. A snapshot holds the blocks of a file after you delete the file.
   This is the usual reason why a large deletion gives you no free space. The
   `overview` command lists the snapshots.
3. **Other APFS volumes use the space.** All of the volumes in one container
   share the same pool. The Preboot volume and the VM volume can therefore use
   your free space. A scan of `/` does not find these volumes.

spacefinder always shows this difference. A total that is short by tens of
gigabytes, with no explanation, is worse than a total with a known gap.

## Directories that stop

Some directories do not fail immediately. Without Full Disk Access, many paths
return `EPERM` at once, and spacefinder counts them as unreadable. But a
permission question with no answer, or a File Provider mount that does not
respond, can stop the system call for an unlimited time. No dialog appears. The
thread cannot be cancelled.

spacefinder therefore monitors its own worker threads. If a directory does not
respond within the `--stall-timeout` period, spacefinder stops that worker,
shows the path, and starts a new worker. The queue then continues.

```
  3 directories timed out - stopped responding, skipped:
      ~/Library/Something
```

spacefinder counts the workers that are alive, not the workers that it started.
A volume with many blocked directories therefore completes.

If many directories stop, the cause is usually a missing permission and not one
bad mount. spacefinder then stops the scan and tells you. More requests in this
condition can remove a permission from your terminal or your editor.

The worker threads are daemon threads. A thread that never returns cannot stop
the program from exiting.

spacefinder does not examine the cloud storage folders by default. These
folders are `~/Library/CloudStorage`, `~/Library/Mobile Documents`,
`~/Dropbox` and `~/OneDrive`. Two problems can occur. A read can stop while it
waits for a sync program. A read can also make macOS download a file that was
only a placeholder. The download uses disk space instead of measuring it. To
examine these folders, use `--include-cloud`.

## rules.json

A rule tells spacefinder where to look, what to match, and what to do:

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
  "note": "Quit Xcode first. The first build after this is slow."
}
```

| field | meaning |
|---|---|
| `paths` | glob patterns. spacefinder expands `~` |
| `find` | `{"names": ["node_modules"], "under": ["~"]}` matches a directory name at any depth |
| `match` | `kind` (dir or file), `min_size_mb`, `older_than_days`, `name_glob` |
| `safety` | `safe`, `caution` or `manual` |
| `action` | `trash`, `command` or `manual`. See "What spacefinder does to your files" |
| `generic` | a general rule. A specific rule for the same path wins |
| `regenerates` | the tests check this field for every `safe` rule |

The three safety levels have these meanings:

- `safe` — the system makes this data again automatically.
- `caution` — you lose a cache that you may want. You must download it or build
  it again.
- `manual` — you must make the decision. spacefinder does not act on these
  rules.

Rules can match the same path. For example, `~/Library/Caches/Homebrew` matches
the specific `homebrew-cache` rule and the general `user-caches` rule.
spacefinder resolves these overlaps before it calculates the total. A specific
rule wins against a general rule. spacefinder removes a match that is inside
another match, because the larger total already contains it. The total
therefore never counts the same bytes two times.

To add knowledge, add a rule to `rules.json`. spacefinder reads only the file
that it ships with. It does not accept a rules file from the command line,
because a rules file controls which files the tool moves.

## What spacefinder does to your files

spacefinder performs one operation only: it moves an item to the Trash. You can
reverse that operation. spacefinder does not delete files, and it does not run
shell commands.

Some rules name a command, such as `brew cleanup -s --prune=all`. spacefinder
shows you the command. You then decide whether to run it. The `clean` command
puts these in a separate list:

```
DRY RUN  25 item(s) to the Trash, 11.5 GB
     1.8 GB  [user-caches]  ~/Library/Caches/ms-playwright
     1.3 GB  [dotcache]  ~/.cache/uv

RUN THESE YOURSELF  spacefinder does not run commands
     2.0 GB  [coresimulator-devices]  ~/Library/Developer/CoreSimulator/Devices
             $ xcrun simctl delete unavailable
```

This limit is deliberate. A tool that can delete a file, or give a line to
`/bin/sh`, needs a correct list of things that it must not touch. Such a list
is never complete. A move that you can reverse does not need one.

## Safety

[SAFETY.md](SAFETY.md) records the full safety model. It lists what the review
considered, what the design closed, and which limits remain.

- The `clean` command does not change anything without `--apply`. It also asks
  for confirmation, unless you add `--yes`.
- The default level is `--safety safe`. At this level spacefinder acts only on
  data that the system makes again. To use a higher level, add
  `--safety caution`.
- spacefinder does not act on `manual` rules. It only shows them.
- spacefinder refuses to move a path outside `~` and `/private/var/folders`. It
  also refuses `/`, your home directory, and the standard folders in your home
  directory.
- spacefinder refuses to move a sensitive folder or anything inside one.
  Examples are `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config` and
  `~/Library/Keychains`.
- spacefinder writes each move to `~/.spacefinder-trash.log`. The log holds the
  original path and the new path.

### The limits of "reversible"

The Trash holds the item, but two limits apply.

The space does not become available until you empty the Trash. spacefinder
tells you this after every move.

spacefinder moves the item with `rename(2)`. It does not write the metadata
that Finder uses for **Put Back**, so Finder cannot return the item to its
original folder for you. To undo a move, read `~/.spacefinder-trash.log` and
move the item back yourself.

## Options

```
--engine bulk|scandir   select an enumeration engine
--threads N             default: 2 x cores, maximum 16
--cross-device          examine other mounted volumes also. Off by default
--stall-timeout SECS    stop a directory that does not respond. Default 15
--include-cloud         examine the iCloud, Google Drive and Dropbox folders
--min-size MB           ignore a result below this size
--json out.json         write the full results to a file. Mode 0600
--all                   show every result, not the first 12
```

spacefinder always skips `/net` and `/home`, because these autofs paths stop if
a mount is stale. It also skips `/dev` and `/System/Volumes/Data`. The last path
is important. It is a firmlink to `/`, so a scan of both paths counts the same
files two times.

## Tests

```sh
python3 tests.py
```

The tests cover these areas:

- the agreement between the two engines
- the totals, against `du`
- the hard-link count
- the overlap resolution between rules
- the path guard on the `trash` action
- the refusal to move a sensitive folder, such as `~/.ssh`
- the absence of shell command execution
- the escape of control characters in the output
- the root selection for `--fast` and for `clean`
- the behaviour when Full Disk Access is absent

## How this project was made

Claude wrote the code in this repository. Claude is an AI assistant from
Anthropic. Kezia Cousins directed the work with prompts, and made the design
decisions.

Claude Opus 5 did the security review before the first public release. It also
wrote the changes that [SAFETY.md](SAFETY.md) records.

Read the code before you trust it. This advice is true for all code. It is more
important for code that moves your files.

## Licence

MIT. Read [LICENSE](LICENSE).
