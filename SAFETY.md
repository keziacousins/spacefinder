# Safety model

spacefinder finds files that you can remove, and it can move some of them to
the Trash. A tool of this type can destroy data. This document records what the
design does about that, and what it deliberately does not do.

A security review before the first public release found the items below. This
document is the result of that review.

## The one rule

**spacefinder performs one operation on your files. It moves an item to the
Trash.**

It does not delete files. It does not run shell commands. It does not accept a
rules file from the command line. Rules that name a command show you the
command, and you decide whether to run it.

This limit does the work that a list of forbidden paths cannot. Such a list is
never complete. The review wrote a rules file that reached
`~/Library/Keychains` and `~/.ssh`. That file passed every guard. The guard
measured path depth, and credentials are deep. A move that you can reverse does
not need a complete list.

## Closed: capability removed

The strongest answer to a risk is the absence of the feature.

- [x] **Shell command execution.** A rule gave its `command` field to `/bin/sh`
      exactly as written. A rules file was therefore equal to a shell script.
      spacefinder now prints the command and runs nothing. This also removed
      the inherited `PATH`, the timeout that left grandchild processes alive,
      and the unlimited output buffer.
- [x] **Permanent delete.** The `delete` action called `shutil.rmtree`. No rule
      used it. The action is gone.
- [x] **Rules files from the command line.** The `--rules` option let any file
      control which files the tool moved. spacefinder now reads only the
      `rules.json` that it ships with.

## Closed: guarded

- [x] **Movement outside a known area.** A target must resolve inside `~` or
      `/private/var/folders`. It must sit at least two levels below that root.
- [x] **Movement of a protected folder.** spacefinder refuses `/`, your home
      directory, and the standard folders inside it.
- [x] **Movement of credentials.** spacefinder refuses `~/.ssh`, `~/.gnupg`,
      `~/.aws`, `~/.kube`, `~/.docker`, `~/.config`, `~/Library/Keychains`,
      `~/Library/Cookies` and other sensitive folders, with everything inside
      them. Depth alone permitted all of these.
- [x] **Escape through a symlinked parent.** The guard resolves the parent
      chain. The move then uses open directory handles, with `O_NOFOLLOW`, so a
      parent that changes between the check and the move cannot defeat it.
- [x] **A symlinked trash folder.** A volume that you do not control can hold a
      `.Trashes` symlink. spacefinder refuses to move an item into it.
- [x] **A copy across volumes.** A move to another volume becomes a copy and
      then a delete. On a disk-space tool this writes the same number of bytes
      that it tries to make available. spacefinder uses the trash folder of the
      volume that holds the item, and refuses a move across volumes.
- [x] **A destination that exists.** A rename onto an existing directory moves
      the item inside it. spacefinder finds a free name first, and it checks
      each candidate.

## Closed: output you can trust

- [x] **Control characters in a file name.** A file name can hold ESC and CR.
      This report is what you read before you decide to remove something, so a
      file name could rewrite or erase the line that describes it. spacefinder
      escapes every control character before it prints a path. It does the same
      for text that comes from `rules.json`.
- [x] **A dry run that hides the action.** The dry run showed only the path for
      a command rule. It now shows the command, in a separate list from the
      items that move to the Trash.
- [x] **A total that counts bytes two times.** Rules overlap. spacefinder
      resolves the overlaps before it calculates the total, so the figure never
      promises space that does not exist.

## Closed: damage to your system

- [x] **A permission storm.** Without Full Disk Access, a protected directory
      does not refuse access. The system call stops and waits for an answer.
      spacefinder answered a stopped directory with a new worker thread, and
      one scan could leave approximately 128 threads in outstanding requests.
      macOS records these against the application that started the scan. During
      this review, that behaviour cost the host editor its Documents
      permission. spacefinder now does three things. It skips the protected
      folders when the permission is absent. It stops the scan if many
      directories stop. It names the correct application in its warning.

## Accepted limits

These are true today. Read them before you trust the tool with something that
matters.

**The Trash does not return space.** The bytes stay on disk until you empty the
Trash. spacefinder says this after every move.

**Finder cannot Put Back an item.** spacefinder moves an item with
`rename(2)`. It does not write the metadata that Finder uses for **Put Back**.
To undo a move, read `~/.spacefinder-trash.log` and move the item back
yourself. The log holds the original path and the new path, and its mode
is 0600.

**The list of sensitive folders is not complete.** No such list is complete.
The list closes the paths that the review found. Read the dry run before you
use `--apply`.

**A dry run and an `--apply` run are separate.** Each run examines the disk
again. If the disk changes between the two runs, the second list is not the
first list. Within one `--apply` run, the list that you see is the list that
moves.

**The free-space figure covers the boot volume only.** A move on another volume
does not appear in it.

**A hard link needs every link removed.** If a file has more than one link, a
move of one link returns no space. The size in the report does not show this.

**`rules.json` is part of the program.** Anybody who can write to the
installation can change which files the tool moves. Treat the file as code.

## Report a problem

Open an issue on GitHub. If the problem lets spacefinder move or destroy a file
that the rules do not name, please say so in the title.
