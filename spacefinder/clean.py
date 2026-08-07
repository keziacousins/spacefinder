"""Actions. Dry-run by default; nothing here runs without --apply."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass

from .sysinfo import human


@dataclass
class Outcome:
    path: str
    action: str
    ok: bool
    detail: str = ""
    freed: int = 0


def _unique(dest: str) -> str:
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    return f"{base} {time.strftime('%H-%M-%S')}{ext}"


def trash(path: str, dry_run: bool = True) -> Outcome:
    """Move *path* into ~/.Trash.

    Deliberately reversible.  Note this does NOT free space until the Trash is
    emptied -- the CLI says so rather than reporting a reclaim that hasn't
    happened.
    """
    dest_dir = os.path.expanduser("~/.Trash")
    dest = _unique(os.path.join(dest_dir, os.path.basename(path.rstrip("/"))))
    if dry_run:
        return Outcome(path, "trash", True, f"would move -> {dest}")
    try:
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(path, dest)
        return Outcome(path, "trash", True, f"moved -> {dest}")
    except OSError as exc:
        return Outcome(path, "trash", False, str(exc))


def delete(path: str, dry_run: bool = True) -> Outcome:
    if dry_run:
        return Outcome(path, "delete", True, "would delete permanently")
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
        else:
            shutil.rmtree(path)
        return Outcome(path, "delete", True, "deleted")
    except OSError as exc:
        return Outcome(path, "delete", False, str(exc))


def run_command(cmd: str, dry_run: bool = True) -> Outcome:
    if dry_run:
        return Outcome(cmd, "command", True, "would run")
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=1800)
        tail = (p.stdout or p.stderr or "").strip().splitlines()
        return Outcome(cmd, "command", p.returncode == 0,
                       tail[-1] if tail else f"exit {p.returncode}")
    except (OSError, subprocess.SubprocessError) as exc:
        return Outcome(cmd, "command", False, str(exc))


def apply_finding(finding, dry_run: bool = True) -> Outcome:
    kind = finding.rule.action.get("kind", "manual")
    if kind == "trash":
        return trash(finding.path, dry_run)
    if kind == "delete":
        return delete(finding.path, dry_run)
    if kind == "command":
        return run_command(finding.rule.action["command"], dry_run)
    return Outcome(finding.path, "manual", True,
                   finding.rule.note or "needs a human decision")


def free_space(mount: str = "/System/Volumes/Data") -> int:
    st = os.statvfs(mount)
    return st.f_bavail * st.f_frsize


def report_outcomes(outcomes: list[Outcome], before: int, dry_run: bool):
    ok = [o for o in outcomes if o.ok]
    bad = [o for o in outcomes if not o.ok]
    if dry_run:
        print(f"\n{len(ok)} action(s) would run. Re-run with --apply to execute.")
    else:
        delta = free_space() - before
        print(f"\n{len(ok)} action(s) succeeded, {len(bad)} failed.")
        print(f"Free space change: {human(delta)}")
    for o in bad:
        print(f"  FAILED {o.path}: {o.detail}")
