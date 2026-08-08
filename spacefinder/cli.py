"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, clean, report, rules as rules_mod, sysinfo
from .scan import Scanner


def _scanner(args, progress=True, has_fda=None):
    return Scanner(
        threads=args.threads,
        engine=args.engine,
        top_files=args.top_files,
        one_filesystem=not args.cross_device,
        stall_timeout=args.stall_timeout,
        include_cloud=args.include_cloud,
        has_fda=has_fda,
        progress=report.progress_printer() if progress else None,
    )


def _warn_tcc(has_fda=None):
    # Only on a definite denial; None means we could not tell, and guessing
    # produces a scary warning on a correctly configured machine.
    if has_fda is None:
        has_fda = sysinfo.full_disk_access()
    if has_fda is not False:
        return
    app = sysinfo.responsible_app()
    target = f"to {app}" if app else "to the application running this shell"
    print(f"{report.YEL}Note:{report.RESET} no Full Disk Access - Mail, "
          f"Messages and some Library folders are skipped.\n"
          f"  Grant it {target} in System Settings > Privacy & Security > "
          f"Full Disk Access.\n"
          f"  {report.DIM}macOS records the grant against that application, "
          f"not against spacefinder.{report.RESET}", file=sys.stderr)


def cmd_overview(args):
    info = sysinfo.volume_info()
    report.print_overview(info)
    walked = sum(v[2] for v in info.volumes)
    if info.container_size and walked:
        print(f"\n{report.DIM}Next: run 'spacefinder scan --fast'. It "
              f"examines only the locations that the rules name.{report.RESET}")
    return 0


def cmd_scan(args):
    has_fda = sysinfo.full_disk_access()
    _warn_tcc(has_fda)
    if args.fast:
        rs = rules_mod.load_rules()
        roots = rules_mod.fast_roots(rs)
        skipped = rules_mod.deep_rules(rs)
        print(f"{report.DIM}fast mode: {len(roots)} named locations"
              f"{report.RESET}", file=sys.stderr)
        if skipped:
            # Say what is being traded away.  These rules search by directory
            # name, so they can only be answered by a full walk -- reporting
            # "nothing found" without saying so would read as a clean bill.
            print(f"{report.DIM}  not evaluated (need a full walk): "
                  f"{', '.join(r.id for r in skipped)}{report.RESET}",
                  file=sys.stderr)
    else:
        roots = args.paths or [os.path.expanduser("~")]

    sc = _scanner(args, has_fda=has_fda)
    res = sc.scan(roots)
    report.clear_progress()
    report.print_scan(res, top=args.top, max_depth=args.depth,
                      show_files=args.top_files if args.files else 0)

    if not args.fast:
        report.print_accounting(res, sysinfo.volume_info(), has_fda)

    if not args.no_rules:
        rs = rules_mod.load_rules()
        findings = rules_mod.evaluate(rs, res, args.min_size)
        report.print_findings(findings, show_all=args.all)

    if args.json:
        _dump_json(args.json, res, locals().get("findings", []))
        print(f"\nwrote {args.json}")
    return 0


def cmd_rules(args):
    rs = rules_mod.load_rules()
    if args.list:
        for r in rs:
            print(f"{report.SAFETY_COLOUR.get(r.safety,'')}{r.safety:<8}"
                  f"{report.RESET} {report.safe_text(r.id):<26} "
                  f"{report.safe_text(r.title)}")
            print(f"         {report.DIM}{report.safe_text(r.why)}"
                  f"{report.RESET}")
            if r.action.get("kind") == "command":
                print(f"         {report.DIM}$ "
                      f"{report.safe_text(r.action['command'])}{report.RESET}")
        return 0

    has_fda = sysinfo.full_disk_access()
    _warn_tcc(has_fda)
    roots = rules_mod.scan_roots(rs)
    sc = _scanner(args, has_fda=has_fda)
    res = sc.scan(roots)
    report.clear_progress()
    findings = rules_mod.evaluate(rs, res, args.min_size)
    report.print_findings(findings, show_all=args.all)
    if args.json:
        _dump_json(args.json, res, findings)
        print(f"\nwrote {args.json}")
    return 0


def cmd_clean(args):
    rs = rules_mod.load_rules()
    if args.rule:
        wanted = set(args.rule)
        rs = [r for r in rs if r.id in wanted]
        missing = wanted - {r.id for r in rs}
        if missing:
            print(f"unknown rule(s): {', '.join(sorted(missing))}",
                  file=sys.stderr)
            return 2
    max_safety = rules_mod.SAFETY_ORDER[args.safety]
    rs = [r for r in rs if rules_mod.SAFETY_ORDER[r.safety] <= max_safety]
    if not rs:
        print("no rules selected", file=sys.stderr)
        return 2

    sc = _scanner(args, has_fda=sysinfo.full_disk_access())
    # scan_roots, not fast_roots: a rule whose root was skipped finds nothing,
    # and "nothing to do" is indistinguishable from "already clean".
    res = sc.scan(rules_mod.scan_roots(rs))
    report.clear_progress()
    findings = rules_mod.evaluate(rs, res, args.min_size)
    findings = [f for f in findings if f.rule.action.get("kind") != "manual"]

    if not findings:
        print("nothing to do")
        return 0

    # Two lists, because they are two different things: one is what
    # spacefinder will move to the Trash, the other is advice for you to act
    # on. Mixing them in one list invited a single "yes" to cover both.
    movable = [f for f in findings if f.rule.action.get("kind") == "trash"]
    advice = [f for f in findings if f.rule.action.get("kind") == "command"]

    dry = not args.apply
    if movable:
        total = sysinfo.human(sum(f.size for f in movable))
        print(f"\n{report.BOLD}{'DRY RUN' if dry else 'APPLYING'}"
              f"{report.RESET}  {len(movable)} item(s) to the Trash, {total}")
        for f in movable:
            print(f"  {sysinfo.human(f.size):>9}  "
                  f"[{report.safe_text(f.rule.id)}]  "
                  f"{report._shorten(f.path)}")

    if advice:
        print(f"\n{report.BOLD}RUN THESE YOURSELF{report.RESET}  "
              f"{report.DIM}spacefinder does not run commands{report.RESET}")
        for f in advice:
            print(f"  {sysinfo.human(f.size):>9}  "
                  f"[{report.safe_text(f.rule.id)}]  "
                  f"{report._shorten(f.path)}")
            print(f"             {report.CYN}$ "
                  f"{report.safe_text(f.rule.action['command'])}{report.RESET}")

    if not movable:
        return 0

    if not dry and not args.yes:
        try:
            if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\naborted")
            return 1

    before = clean.free_space()
    log = clean.TrashLog() if not dry else None
    outcomes = [clean.apply_finding(f, dry_run=dry, log=log) for f in movable]
    clean.report_outcomes(outcomes, before, dry)
    if not dry and any(o.action == "trash" and o.ok for o in outcomes):
        print(f"{report.YEL}Items are in the Trash. The space is not returned "
              f"until you empty it.{report.RESET}")
        print(f"  {report.DIM}Move an item back yourself to undo. spacefinder "
              f"does not record Finder's Put Back metadata.{report.RESET}")
        written = log.flush() if log else None
        if written:
            print(f"  {report.DIM}Recorded in {written}{report.RESET}")
    return 0


def _dump_json(path, res, findings):
    doc = {
        "roots": res.roots,
        "elapsed_s": round(res.elapsed, 2),
        "engine": res.engine,
        "dirs": res.dir_count,
        "files": res.file_count,
        "total_alloc": res.grand_total,
        "unreadable_dirs": len(res.denied),
        "largest_dirs": [{"path": p, "bytes": s}
                         for s, p in res.top_dirs(200)],
        "largest_files": [{"path": f.path, "bytes": f.alloc,
                           "logical": f.logical} for f in res.big_files],
        "findings": [{"rule": f.rule.id, "safety": f.rule.safety,
                      "path": f.path, "bytes": f.size,
                      "action": f.rule.action} for f in findings],
    }
    # 0600: this is a map of someone's home directory -- project names, app
    # data, message-attachment folders.  Not something to leave world-readable
    # by default, especially when the obvious place to write it is /tmp.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=2)


def build_parser():
    p = argparse.ArgumentParser(
        prog="spacefinder",
        description="Find and reclaim disk space on macOS.")
    p.add_argument("--version", action="version",
                   version=f"spacefinder {__version__}")
    p.add_argument("--threads", type=int)
    p.add_argument("--engine", choices=["bulk", "scandir"],
                   help="force an enumeration engine (default: auto)")
    p.add_argument("--cross-device", action="store_true",
                   help="follow into other mounted volumes")
    p.add_argument("--stall-timeout", type=float, default=15.0,
                   help="give up on a directory that blocks this long (s)")
    p.add_argument("--include-cloud", action="store_true",
                   help="also walk iCloud/Google Drive/Dropbox mounts "
                        "(may block, and may trigger downloads)")
    p.add_argument("--top-files", type=int, default=30)
    p.add_argument("--min-size", type=float, default=0,
                   help="ignore findings below N MB")
    p.add_argument("--all", action="store_true", help="show every finding")
    p.add_argument("--json", help="write full results to a JSON file")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("overview", help="volume, container, snapshots, purgeable")
    o.set_defaults(func=cmd_overview)

    s = sub.add_parser("scan", help="walk the tree and report the biggest items")
    s.add_argument("paths", nargs="*", help="default: ~")
    s.add_argument("--fast", action="store_true",
                   help="scan only the specific paths named by rules; skips "
                        "rules that search by directory name (node_modules "
                        "and friends), which need a full walk")
    s.add_argument("--top", type=int, default=25)
    s.add_argument("--depth", type=int, help="limit reported depth")
    s.add_argument("--files", action="store_true", default=True)
    s.add_argument("--no-files", dest="files", action="store_false")
    s.add_argument("--no-rules", action="store_true")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("rules", help="evaluate the rules file")
    r.add_argument("--list", action="store_true", help="just list the rules")
    r.set_defaults(func=cmd_rules)

    c = sub.add_parser("clean", help="apply rule actions (dry run by default)")
    c.add_argument("--rule", action="append", help="restrict to rule id(s)")
    c.add_argument("--safety", choices=["safe", "caution", "manual"],
                   default="safe", help="highest risk level to act on")
    c.add_argument("--apply", action="store_true", help="actually do it")
    c.add_argument("--yes", action="store_true", help="skip confirmation")
    c.set_defaults(func=cmd_clean)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
