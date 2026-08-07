"""Volume-level facts that a file walk cannot see.

A tree scan can only find files that exist.  On macOS a large share of "where
did my space go" is space that a walk will never attribute: APFS snapshots,
purgeable caches, and the gap between what the container reports and what the
volume's files add up to.  Check these before scanning.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass
class VolumeInfo:
    mount: str = "/System/Volumes/Data"
    total: int = 0
    used: int = 0
    free: int = 0
    container_size: int = 0
    container_free: int = 0
    volumes: list[tuple[str, str, int]] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)
    purgeable: int = 0


def volume_info(mount: str = "/System/Volumes/Data") -> VolumeInfo:
    info = VolumeInfo(mount=mount)

    st = os.statvfs(mount)
    info.total = st.f_blocks * st.f_frsize
    info.free = st.f_bavail * st.f_frsize
    info.used = info.total - (st.f_bfree * st.f_frsize)

    # Container-level numbers: on APFS all volumes share one pool, so a
    # sibling volume (VM swap, Preboot, another APFS volume) can be eating
    # the space your Data volume walk will never see.
    out = _run(["diskutil", "apfs", "list"])
    cur = None
    for line in out.splitlines():
        m = re.search(r"Size \(Capacity Ceiling\):\s+(\d+) B", line)
        if m:
            info.container_size = int(m.group(1))
        m = re.search(r"Capacity Not Allocated:\s+(\d+) B", line)
        if m:
            info.container_free = int(m.group(1))
        m = re.search(r"APFS Volume Disk \(Role\):\s+(\S+)\s+\((\w*)\)", line)
        if m:
            cur = (m.group(1), m.group(2) or "-")
        m = re.search(r"Capacity Consumed:\s+(\d+) B", line)
        if m and cur:
            info.volumes.append((cur[0], cur[1], int(m.group(1))))
            cur = None

    # Local Time Machine snapshots pin deleted files' blocks. Frequently the
    # entire answer to "I deleted 50 GB and nothing came back".
    for line in _run(["tmutil", "listlocalsnapshots", "/"]).splitlines():
        line = line.strip()
        if line.startswith("com.apple.TimeMachine"):
            info.snapshots.append(line)

    # "Purgeable": space macOS reports as free-on-demand. statvfs f_bavail
    # already excludes it, so the gap shows how much is being held back.
    du = shutil.disk_usage(mount)
    info.purgeable = max(0, (du.total - du.used) - du.free)

    return info


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def full_disk_access() -> bool:
    """True if this process can read a TCC-protected location.

    Without Full Disk Access the scan silently misses Mail, Messages and
    several Library subtrees -- worth telling the user rather than reporting a
    total that is quietly too small.
    """
    probe = os.path.expanduser("~/Library/Application Support/com.apple.TCC")
    try:
        os.listdir(probe)
        return True
    except OSError:
        return False
