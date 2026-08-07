"""Fast directory enumeration for macOS.

Two engines:

``bulk``
    Uses ``getattrlistbulk(2)``, the macOS syscall that returns metadata for
    many directory entries at once.  This is the trick behind fast GUI scanners
    like DaisyDisk: instead of ``readdir()`` + one ``lstat()`` per file, we get
    name, type, size, link count and mtime for dozens of entries per syscall.

``scandir``
    Portable fallback built on ``os.scandir``.  Correct but roughly 3-5x more
    syscalls.

The bulk parser hand-decodes a packed C struct, which is precisely the sort of
thing that fails silently and reports plausible-but-wrong sizes.  So
:func:`self_test` validates it against ``os.lstat`` before we trust it, and
:func:`get_engine` falls back to ``scandir`` if anything looks off.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
from typing import Iterator, NamedTuple

# --------------------------------------------------------------------------
# Entry record shared by both engines
# --------------------------------------------------------------------------

DT_FILE = 0
DT_DIR = 1
DT_LINK = 2
DT_OTHER = 3


class Entry(NamedTuple):
    name: str
    kind: int       # DT_* above
    alloc: int      # bytes actually allocated on disk
    logical: int    # apparent size (st_size); differs for sparse/compressed
    nlink: int      # >1 means hard links; dedupe on (dev, ino)
    ino: int
    dev: int
    mtime: float


# --------------------------------------------------------------------------
# getattrlistbulk engine
# --------------------------------------------------------------------------

_libc_path = ctypes.util.find_library("c")
_libc = ctypes.CDLL(_libc_path, use_errno=True) if _libc_path else None

O_DIRECTORY = 0x00100000        # <sys/fcntl.h>, macOS value
O_NOFOLLOW = 0x00000100

ATTR_BIT_MAP_COUNT = 5

# <sys/attr.h>
ATTR_CMN_NAME = 0x00000001
ATTR_CMN_DEVID = 0x00000002
ATTR_CMN_OBJTYPE = 0x00000008
ATTR_CMN_MODTIME = 0x00000400
ATTR_CMN_FILEID = 0x02000000
ATTR_CMN_RETURNED_ATTRS = 0x80000000

ATTR_FILE_LINKCOUNT = 0x00000001
ATTR_FILE_TOTALSIZE = 0x00000002
ATTR_FILE_ALLOCSIZE = 0x00000004

FSOPT_NOFOLLOW = 0x00000001
FSOPT_PACK_INVAL_ATTRS = 0x00000008   # always emit every requested attr

# vnode types from <sys/vnode.h>
VREG, VDIR, VLNK = 1, 2, 5

_COMMON = ATTR_CMN_RETURNED_ATTRS | ATTR_CMN_NAME | ATTR_CMN_DEVID | \
    ATTR_CMN_OBJTYPE | ATTR_CMN_MODTIME | ATTR_CMN_FILEID
_FILE = ATTR_FILE_LINKCOUNT | ATTR_FILE_TOTALSIZE | ATTR_FILE_ALLOCSIZE
_OPTIONS = FSOPT_NOFOLLOW | FSOPT_PACK_INVAL_ATTRS


class _Attrlist(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


# Each record is:
#   uint32     entry length
#   uint32[5]  ATTR_CMN_RETURNED_ATTRS   (always first when requested)
#   ...requested attributes, packed in ascending bit order, no padding
#
# Crucially the kernel only emits attributes that *apply to that entry*: a
# directory carries no ATTR_FILE_* fields, so records are not a fixed size.
# (FSOPT_PACK_INVAL_ATTRS zero-fills attrs that are merely unavailable, not
# ones that are inapplicable.)  We therefore build a decode plan per distinct
# returned-attrs bitmap instead of assuming one layout -- getting this wrong
# reads into the following record and yields plausible but wildly wrong sizes.

# (bit, struct code, byte width, field key), in ascending bit order.
_CMN_FIELDS = [
    (ATTR_CMN_NAME, "iI", 8, "name"),      # attrreference_t
    (ATTR_CMN_DEVID, "i", 4, "dev"),
    (ATTR_CMN_OBJTYPE, "I", 4, "objtype"),
    (ATTR_CMN_MODTIME, "qq", 16, "mtime"),  # struct timespec
    (ATTR_CMN_FILEID, "Q", 8, "ino"),
]
_FILE_FIELDS = [
    (ATTR_FILE_LINKCOUNT, "I", 4, "nlink"),
    (ATTR_FILE_TOTALSIZE, "q", 8, "logical"),
    (ATTR_FILE_ALLOCSIZE, "q", 8, "alloc"),
]

_ATTRS_START = 4 + 20   # length + returned_attrs
_plan_cache: dict[tuple[int, int], tuple] = {}


def _build_plan(cmn_ret: int, file_ret: int) -> tuple:
    """Return (Struct, idx_map, name_byte_offset) for one returned-attrs shape."""
    fmt = "<"
    idx = 0
    boff = 0
    idx_map: dict[str, int] = {}
    name_boff = -1
    for group, requested, fields in (
            ("cmn", cmn_ret & _COMMON, _CMN_FIELDS),
            ("file", file_ret & _FILE, _FILE_FIELDS)):
        for bit, code, size, key in fields:
            if not (requested & bit):
                continue
            if key == "name":
                name_boff = boff
            fmt += code
            idx_map[key] = idx
            idx += len(code)
            boff += size
    return struct.Struct(fmt), idx_map, name_boff


_BUFSIZE = 256 * 1024


def _bulk_listdir(path: str, buf: ctypes.Array) -> Iterator[Entry]:
    """Yield entries of *path* using getattrlistbulk. Raises OSError."""
    fd = os.open(path, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    try:
        alist = _Attrlist(ATTR_BIT_MAP_COUNT, 0, _COMMON, 0, 0, _FILE, 0)
        view = memoryview(buf)
        head = struct.Struct("<I 5I")
        while True:
            n = _libc.getattrlistbulk(
                fd, ctypes.byref(alist), buf, _BUFSIZE, _OPTIONS)
            if n < 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err), path)
            if n == 0:
                return
            off = 0
            for _ in range(n):
                length, cmn_ret, _vol, _dir, file_ret, _fork = \
                    head.unpack_from(view, off)

                plan = _plan_cache.get((cmn_ret, file_ret))
                if plan is None:
                    plan = _build_plan(cmn_ret, file_ret)
                    _plan_cache[(cmn_ret, file_ret)] = plan
                rec, ix, name_boff = plan

                vals = rec.unpack_from(view, off + _ATTRS_START)

                i = ix.get("name")
                if i is None:
                    off += length
                    continue
                name_off, name_len = vals[i], vals[i + 1]
                start = off + _ATTRS_START + name_boff + name_off
                name = bytes(view[start:start + name_len - 1]).decode(
                    "utf-8", "surrogateescape")

                objtype = vals[ix["objtype"]] if "objtype" in ix else 0
                if objtype == VDIR:
                    kind = DT_DIR
                elif objtype == VREG:
                    kind = DT_FILE
                elif objtype == VLNK:
                    kind = DT_LINK
                else:
                    kind = DT_OTHER

                # Only regular files carry bytes.  Directories return no
                # ATTR_FILE_* at all; symlinks do return them (their data fork
                # is the target path) but we don't want that counted.  The
                # scandir engine applies the same rule so the two agree.
                if kind == DT_FILE:
                    alloc = vals[ix["alloc"]] if "alloc" in ix else 0
                    logical = vals[ix["logical"]] if "logical" in ix else 0
                    nlink = vals[ix["nlink"]] if "nlink" in ix else 1
                else:
                    alloc = logical = 0
                    nlink = 1

                yield Entry(
                    name, kind, alloc, logical, nlink,
                    vals[ix["ino"]] if "ino" in ix else 0,
                    vals[ix["dev"]] if "dev" in ix else 0,
                    float(vals[ix["mtime"]]) if "mtime" in ix else 0.0,
                )
                off += length
    finally:
        os.close(fd)


def bulk_listdir(path: str) -> list[Entry]:
    buf = ctypes.create_string_buffer(_BUFSIZE)
    return list(_bulk_listdir(path, buf))


# --------------------------------------------------------------------------
# scandir engine
# --------------------------------------------------------------------------

def scandir_listdir(path: str) -> list[Entry]:
    out = []
    with os.scandir(path) as it:
        for de in it:
            try:
                st = de.stat(follow_symlinks=False)
            except OSError:
                continue
            if de.is_dir(follow_symlinks=False):
                kind = DT_DIR
            elif de.is_file(follow_symlinks=False):
                kind = DT_FILE
            elif de.is_symlink():
                kind = DT_LINK
            else:
                kind = DT_OTHER
            if kind == DT_FILE:
                alloc, logical, nlink = st.st_blocks * 512, st.st_size, st.st_nlink
            else:
                # Match the bulk engine: only file bytes are accounted.
                alloc, logical, nlink = 0, 0, 1
            out.append(Entry(de.name, kind, alloc, logical, nlink,
                             st.st_ino, st.st_dev, st.st_mtime))
    return out


# --------------------------------------------------------------------------
# Validation + engine selection
# --------------------------------------------------------------------------

class SelfTestError(Exception):
    pass


def self_test(sample_dirs: list[str] | None = None) -> None:
    """Cross-check the bulk parser against os.lstat. Raises SelfTestError."""
    if _libc is None or not hasattr(_libc, "getattrlistbulk"):
        raise SelfTestError("getattrlistbulk unavailable in libc")

    _libc.getattrlistbulk.argtypes = [
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_uint64]
    _libc.getattrlistbulk.restype = ctypes.c_int

    if sample_dirs is None:
        sample_dirs = [os.path.expanduser("~"), "/usr/lib", "/System/Library/CoreServices"]

    checked = 0
    for d in sample_dirs:
        if not os.path.isdir(d):
            continue
        try:
            bulk = {e.name: e for e in bulk_listdir(d)}
        except OSError:
            continue
        ref = {e.name: e for e in scandir_listdir(d)}

        # Names must agree (scandir may drop entries that vanished mid-scan).
        missing = set(ref) - set(bulk)
        if missing:
            raise SelfTestError(f"{d}: bulk missed {sorted(missing)[:3]}")

        for name, r in ref.items():
            b = bulk[name]
            if b.kind != r.kind:
                raise SelfTestError(f"{d}/{name}: kind {b.kind} != {r.kind}")
            if b.ino != r.ino:
                raise SelfTestError(f"{d}/{name}: ino {b.ino} != {r.ino}")
            if b.dev != r.dev:
                raise SelfTestError(f"{d}/{name}: dev {b.dev} != {r.dev}")
            # Checked for *every* entry, not just files: the failure mode this
            # guards against is a misparsed record length on non-file entries
            # spilling garbage sizes into the totals.
            if b.nlink != r.nlink:
                raise SelfTestError(f"{d}/{name}: nlink {b.nlink} != {r.nlink}")
            if b.logical != r.logical:
                raise SelfTestError(
                    f"{d}/{name}: size {b.logical} != {r.logical}")
            # Allocated size may legitimately differ by a block for
            # compressed files; require the same order of magnitude.
            if abs(b.alloc - r.alloc) > max(65536, r.alloc * 0.5):
                raise SelfTestError(f"{d}/{name}: alloc {b.alloc} != {r.alloc}")
            checked += 1

    if checked < 5:
        raise SelfTestError("not enough entries to validate")


def get_engine(force: str | None = None):
    """Return (name, listdir_callable)."""
    if force == "scandir":
        return "scandir", scandir_listdir
    try:
        self_test()
    except SelfTestError as exc:
        if force == "bulk":
            raise
        return f"scandir (bulk unavailable: {exc})", scandir_listdir
    return "bulk", bulk_listdir
