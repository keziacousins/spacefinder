"""Rules: codified local knowledge about what is safe to reclaim.

A rule says *where* to look, *what* counts as a hit, and *what to do* about it.
Rules are matched against an already-computed :class:`~spacefinder.scan.ScanResult`
where possible, so a full scan answers every rule without re-walking.
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from dataclasses import dataclass, field

SAFETY_ORDER = {"safe": 0, "caution": 1, "manual": 2}


@dataclass
class Rule:
    id: str
    title: str
    why: str = ""
    category: str = "other"
    safety: str = "caution"           # safe | caution | manual
    paths: list[str] = field(default_factory=list)   # globs, ~ expanded
    find_names: list[str] = field(default_factory=list)  # dir names anywhere
    find_under: list[str] = field(default_factory=list)
    kind: str = "dir"                 # dir | file
    name_glob: str = ""
    min_size_mb: float = 0.0
    older_than_days: float = 0.0
    action: dict = field(default_factory=lambda: {"kind": "manual"})
    regenerates: bool = False
    generic: bool = False    # catch-all; loses to a specific rule on same path
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        m = d.get("match", {})
        a = d.get("action", {"kind": "manual"})
        return cls(
            id=d["id"],
            title=d.get("title", d["id"]),
            why=d.get("why", ""),
            category=d.get("category", "other"),
            safety=d.get("safety", "caution"),
            paths=list(d.get("paths", [])),
            find_names=list(d.get("find", {}).get("names", [])),
            find_under=list(d.get("find", {}).get("under", [])),
            kind=m.get("kind", "dir"),
            name_glob=m.get("name_glob", ""),
            min_size_mb=float(m.get("min_size_mb", 0)),
            older_than_days=float(m.get("older_than_days", 0)),
            action=a,
            regenerates=bool(d.get("regenerates", False)),
            generic=bool(d.get("generic", False)),
            note=d.get("note", ""),
        )

    @staticmethod
    def _trim(p: str) -> str:
        """The containing directory of a glob pattern."""
        p = os.path.expanduser(p)
        parts = []
        for seg in p.split("/"):
            if any(ch in seg for ch in "*?["):
                break
            parts.append(seg)
        return "/".join(parts) or "/"

    def path_roots(self) -> list[str]:
        """Directories holding this rule's explicit `paths` globs.

        Bounded and cheap: each is a named location a few levels deep.
        """
        return [r for r in map(self._trim, self.paths) if os.path.isdir(r)]

    def find_roots(self) -> list[str]:
        """Directories that must be walked in full to answer `find.names`.

        Unbounded: finding every node_modules under ~ means walking ~.
        """
        return [r for r in map(self._trim, self.find_under) if os.path.isdir(r)]


@dataclass
class Finding:
    rule: Rule
    path: str
    size: int
    mtime: float = 0.0

    @property
    def age_days(self) -> float:
        return (time.time() - self.mtime) / 86400 if self.mtime else 0.0


def load_rules(path: str | None = None) -> list[Rule]:
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "rules.json")
    with open(path) as fh:
        doc = json.load(fh)
    return [Rule.from_dict(r) for r in doc.get("rules", [])]


def _size_of(path: str, res, want_dir: bool):
    """Size + mtime for *path*, preferring the scan's rolled-up total."""
    if want_dir and path in res.total_alloc:
        try:
            return res.total_alloc[path], os.lstat(path).st_mtime
        except OSError:
            return res.total_alloc[path], 0.0
    try:
        st = os.lstat(path)
    except OSError:
        return None, 0.0
    if want_dir:
        if not os.path.isdir(path):
            return None, 0.0
        return None, st.st_mtime           # dir not in scan; caller re-scans
    return st.st_blocks * 512, st.st_mtime


def evaluate(rules: list[Rule], res, min_size_mb: float = 0.0) -> list[Finding]:
    """Match *rules* against a scan result. Returns findings, largest first."""
    findings: list[Finding] = []
    dirs_by_name: dict[str, list[str]] = {}
    for p in res.total_alloc:
        dirs_by_name.setdefault(os.path.basename(p), []).append(p)

    for rule in rules:
        hits: list[tuple[str, int, float]] = []

        # 1. Explicit path globs.
        for pattern in rule.paths:
            pattern = os.path.expanduser(pattern)
            for path in _iglob(pattern, res, rule.kind):
                size, mtime = _size_of(path, res, rule.kind == "dir")
                if size is None:
                    continue
                hits.append((path, size, mtime))

        # 2. Directories matched by name anywhere under a root, e.g.
        #    every node_modules under ~/src.
        for name in rule.find_names:
            for path in dirs_by_name.get(name, ()):
                if rule.find_under and not any(
                        path.startswith(os.path.expanduser(u).rstrip("/") + "/")
                        for u in rule.find_under):
                    continue
                try:
                    mtime = os.lstat(path).st_mtime
                except OSError:
                    mtime = 0.0
                hits.append((path, res.total_alloc.get(path, 0), mtime))

        # Nested hits of the same rule (node_modules inside node_modules) are
        # already inside the outer one's rolled-up total -- counting both would
        # overstate what you get back.
        hits.sort(key=lambda h: len(h[0]))
        kept: list[tuple[str, int, float]] = []
        for path, size, mtime in hits:
            if any(path.startswith(k[0].rstrip("/") + "/") for k in kept):
                continue
            kept.append((path, size, mtime))

        floor = max(rule.min_size_mb, min_size_mb) * 1024 * 1024
        now = time.time()
        for path, size, mtime in kept:
            if size < floor:
                continue
            if rule.name_glob and not fnmatch.fnmatch(os.path.basename(path),
                                                      rule.name_glob):
                continue
            if rule.older_than_days and mtime and \
                    (now - mtime) < rule.older_than_days * 86400:
                continue
            findings.append(Finding(rule, path, size, mtime))

    return _resolve_overlaps(findings)


def _resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """Ensure no byte is reported twice.

    Rules overlap by design -- ~/Library/Caches/Homebrew matches both the
    specific 'homebrew-cache' rule and the catch-all 'user-caches' rule, and
    a nested hit is already inside its ancestor's rolled-up total.  Without
    this pass the "safe to reclaim" figure is inflated, which is worse than
    useless: it promises space that does not exist.
    """
    best: dict[str, Finding] = {}
    for f in findings:
        prev = best.get(f.path)
        if prev is None or (prev.rule.generic and not f.rule.generic):
            best[f.path] = f

    kept: list[Finding] = []
    kept_paths: set[str] = set()
    # Shallowest first, so an ancestor is always decided before its children.
    for f in sorted(best.values(), key=lambda f: (f.path.count("/"), f.path)):
        parent = os.path.dirname(f.path)
        covered = False
        while len(parent) > 1:
            if parent in kept_paths:
                covered = True
                break
            parent = os.path.dirname(parent)
        if not covered:
            kept.append(f)
            kept_paths.add(f.path)

    kept.sort(key=lambda f: -f.size)
    return kept


def _iglob(pattern: str, res, kind: str):
    """Glob, preferring scan data over hitting the filesystem."""
    import glob as _glob
    if any(ch in pattern for ch in "*?["):
        return _glob.iglob(pattern, recursive="**" in pattern)
    return [pattern] if os.path.exists(pattern) else []


def _dedupe(roots) -> list[str]:
    """Deduped, non-overlapping, shallowest-wins."""
    out: list[str] = []
    for r in sorted(set(os.path.realpath(r) for r in roots)):
        if any(r == o or r.startswith(o.rstrip("/") + "/") for o in out):
            continue
        out.append(r)
    return out


def fast_roots(rules: list[Rule]) -> list[str]:
    """The cheap, explicitly-named hot spots.

    Only rules that name `paths` are covered.  A `find.names` rule -- every
    node_modules under ~ -- has no hot spot to visit: answering it means
    walking the whole subtree.  Folding its `find.under` root in here is what
    used to make --fast expand to a full home-directory walk while still
    reporting "known hot spots", so the two root sets are now kept apart.
    """
    return _dedupe(r for rule in rules for r in rule.path_roots())


def scan_roots(rules: list[Rule]) -> list[str]:
    """Everything that must be walked to evaluate *rules* correctly.

    Use this wherever a wrong answer matters more than the wait -- notably
    `clean`, where a missed root means a rule silently finds nothing.
    """
    return _dedupe([r for rule in rules for r in rule.path_roots()]
                   + [r for rule in rules for r in rule.find_roots()])


def deep_rules(rules: list[Rule]) -> list[Rule]:
    """Rules that :func:`fast_roots` cannot answer."""
    return [r for r in rules if r.find_names]
