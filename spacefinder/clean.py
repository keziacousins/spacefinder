"""Actions.

spacefinder performs exactly one operation on your files: it moves an item to
the Trash, which you can undo.  It never deletes and it never runs a shell
command.  Rules that need a command print the command for you to read and run
yourself.

That boundary is deliberate.  Every serious hole found in review lived in the
action path, and reversibility is the only line that holds up: a guard list can
be wrong, but a move you can reverse cannot cost you data.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .sysinfo import human

# Actions may only touch paths inside these roots.  rules.json is data, but it
# drives file movement, so a mistaken `paths` entry must not reach `/` or a
# home directory itself.  The check lives here rather than in the rule layer
# because this is the last point before the filesystem changes.
_ALLOWED_ROOTS = ("~", "/private/var/folders")

# Never acted on, whatever a rule says.
_DENIED = (
    "/", "/private/var/folders",
    "~", "~/Library", "~/Documents", "~/Desktop", "~/Downloads",
    "~/Pictures", "~/Music", "~/Movies", "~/Applications", "~/.Trash",
)

# Never acted on, along with everything underneath them.  Depth alone is the
# wrong axis: it refuses ~/.zshrc (one level down) while permitting
# ~/Library/Keychains/login.keychain-db (three).  These are the subtrees where
# a mistake costs credentials rather than a cache, so they are named outright.
_SENSITIVE = (
    "~/.ssh", "~/.gnupg", "~/.aws", "~/.kube", "~/.docker", "~/.config",
    "~/.password-store", "~/.local/share/keyrings",
    "~/Library/Keychains", "~/Library/Cookies", "~/Library/Accounts",
    "~/Library/Application Support/com.apple.TCC",
    "~/Library/Application Support/MobileSync",
    "~/Library/Messages", "~/Library/Mail",
)

# A target must sit at least this many components below its allowed root.
# Everything rules.json legitimately targets is two or more deep; a direct
# child of home never is.
_MIN_DEPTH = 2


@dataclass
class Outcome:
    path: str
    action: str
    ok: bool
    detail: str = ""
    freed: int = 0


def _real(path: str) -> str:
    """Resolve *path* without following a symlink at the leaf.

    The containing chain is fully resolved, so a symlinked parent cannot be
    used to escape the allowed roots.  The leaf is left alone: trashing a
    symlink should move the link, not whatever it points at.
    """
    p = os.path.abspath(path.rstrip("/") or "/")
    parent = os.path.realpath(os.path.dirname(p))
    return os.path.join(parent, os.path.basename(p))


def check_path(path: str) -> str | None:
    """Return a refusal reason for *path*, or None if it is safe to act on."""
    target = _real(path)
    for denied in _DENIED:
        if target == os.path.realpath(os.path.expanduser(denied)):
            return "protected location"
    for sensitive in _SENSITIVE:
        s = os.path.realpath(os.path.expanduser(sensitive)).rstrip("/")
        if target == s or target.startswith(s + "/"):
            return f"sensitive location ({sensitive})"
    for root in _ALLOWED_ROOTS:
        root = os.path.realpath(os.path.expanduser(root)).rstrip("/")
        if not target.startswith(root + "/"):
            continue
        if target[len(root) + 1:].count("/") + 1 < _MIN_DEPTH:
            return f"too close to the root of {root}"
        return None
    return "outside the allowed roots (~ and /private/var/folders)"


def _unique(dest: str) -> str:
    """A destination that does not exist yet.

    Must genuinely not exist: a rename onto an existing directory would move
    the source *inside* it, which misplaces the item instead of failing.
    """
    if not os.path.lexists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    stamp = time.strftime("%Y-%m-%d %H-%M-%S")
    for n in range(1, 1000):
        cand = f"{base} {stamp}{ext}" if n == 1 else f"{base} {stamp} ({n}){ext}"
        if not os.path.lexists(cand):
            return cand
    raise OSError(f"no free name for {dest}")


def _mount_point(path: str) -> str:
    p = os.path.dirname(_real(path))
    dev = os.stat(p).st_dev
    while p != "/":
        parent = os.path.dirname(p)
        if os.stat(parent).st_dev != dev:
            return p
        p = parent
    return "/"


def _trash_dir(path: str) -> str:
    """The trash directory serving *path*'s volume. Raises OSError.

    macOS keeps a per-volume trash.  Using ~/.Trash for a path on another
    volume would turn the move into copy-then-delete: on a disk-space tool,
    writing 40 GB to the boot volume in order to reclaim 40 GB, and leaving a
    partial copy behind if it fails halfway.
    """
    home = os.path.expanduser("~")
    if os.stat(os.path.dirname(_real(path))).st_dev == os.stat(home).st_dev:
        return os.path.join(home, ".Trash")
    return os.path.join(_mount_point(path), ".Trashes", str(os.getuid()))


def _open_trash_dir(dest_dir: str) -> int:
    """Open *dest_dir*, creating it, and refuse to follow a symlink.

    A per-volume .Trashes lives on media we do not control -- a USB stick, a
    mounted image, a shared drive.  If .Trashes or the uid directory under it
    is a symlink, an unchecked move lands wherever it points.
    """
    parent = os.path.dirname(dest_dir)
    if parent != dest_dir and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    if os.path.islink(dest_dir):
        raise OSError(f"{dest_dir} is a symlink; refusing to trash into it")
    os.makedirs(dest_dir, exist_ok=True)
    fd = os.open(dest_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not os.path.samestat(os.fstat(fd), os.stat(dest_dir)):
            raise OSError(f"{dest_dir} changed while opening it")
    except OSError:
        os.close(fd)
        raise
    return fd


def trash(path: str, dry_run: bool = True, log=None) -> Outcome:
    """Move *path* into the trash of its own volume.

    Reversible by hand: the item keeps its name and contents.  Note this does
    NOT free space until the Trash is emptied, and it does not record the
    metadata Finder uses for "Put Back", so the item will not restore itself to
    its original location.  The CLI says both rather than overstating the undo.
    """
    reason = check_path(path)
    if reason:
        return Outcome(path, "trash", False, f"refused: {reason}")
    try:
        dest_dir = _trash_dir(path)
        name = os.path.basename(path.rstrip("/"))
        dest = _unique(os.path.join(dest_dir, name))
    except OSError as exc:
        return Outcome(path, "trash", False, str(exc))
    if dry_run:
        return Outcome(path, "trash", True, f"would move -> {dest}")

    src_parent = os.path.dirname(_real(path))
    src_fd = dest_fd = None
    try:
        # Operate relative to open directory handles.  check_path resolved the
        # parent chain a moment ago; without this, a parent swapped for a
        # symlink between the check and the rename would defeat it.
        src_fd = os.open(src_parent, os.O_RDONLY | os.O_DIRECTORY |
                         os.O_NOFOLLOW)
        dest_fd = _open_trash_dir(dest_dir)
        if os.fstat(src_fd).st_dev != os.fstat(dest_fd).st_dev:
            raise OSError(f"{dest_dir} is on another volume; refusing to copy "
                          f"rather than move")
        os.rename(name, os.path.basename(dest),
                  src_dir_fd=src_fd, dst_dir_fd=dest_fd)
        if log is not None:
            log.record(path, dest)
        return Outcome(path, "trash", True, f"moved -> {dest}")
    except OSError as exc:
        return Outcome(path, "trash", False, str(exc))
    finally:
        for fd in (src_fd, dest_fd):
            if fd is not None:
                os.close(fd)


def advise(cmd: str) -> Outcome:
    """Report a rule's shell one-liner. spacefinder never runs it.

    Running these meant handing `action.command` to /bin/sh as written, which
    made a rules file equivalent to a shell script and dragged in inherited
    PATH, orphaned grandchildren on timeout, and unbounded output capture.
    None of that buys anything: these are one-liners a developer can read and
    run in less time than it takes to audit a consent flag.
    """
    return Outcome(cmd, "advice", True, "run this yourself")


def apply_finding(finding, dry_run: bool = True, log=None) -> Outcome:
    kind = finding.rule.action.get("kind", "manual")
    if kind == "trash":
        return trash(finding.path, dry_run, log)
    if kind == "command":
        return advise(finding.rule.action["command"])
    return Outcome(finding.path, "manual", True,
                   finding.rule.note or "needs a human decision")


class TrashLog:
    """Append-only record of what moved where.

    The undo story is "move it back", which needs both paths.  Terminal
    scrollback is not a record.
    """

    def __init__(self, path: str | None = None):
        self.path = path or os.path.expanduser("~/.spacefinder-trash.log")
        self.entries: list[tuple[str, str]] = []

    def record(self, src: str, dest: str):
        self.entries.append((src, dest))

    def flush(self):
        if not self.entries:
            return None
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            for src, dest in self.entries:
                fh.write(f"{stamp}\t{src}\t{dest}\n")
        return self.path


def free_space(mount: str = "/System/Volumes/Data") -> int:
    st = os.statvfs(mount)
    return st.f_bavail * st.f_frsize


def report_outcomes(outcomes: list[Outcome], before: int, dry_run: bool):
    from .report import safe_text
    advice = [o for o in outcomes if o.action == "advice"]
    ok = [o for o in outcomes if o.ok and o.action != "advice"]
    bad = [o for o in outcomes if not o.ok]
    if dry_run:
        print(f"\n{len(ok)} item(s) would move to the Trash. "
              f"Re-run with --apply.")
    else:
        # Only meaningful for the boot volume; a move on another volume does
        # not show up here at all, and the Trash holds the bytes regardless.
        delta = free_space() - before
        print(f"\n{len(ok)} item(s) moved, {len(bad)} failed.")
        print(f"Free space change on the boot volume: {human(delta)}")
    for o in bad:
        print(f"  FAILED {safe_text(o.path)}: {o.detail}")
