"""Command-line interface: argument parsing and subcommand dispatch."""

import argparse

from .core import log
from .pipeline import do_analyse, do_capture, do_post, do_preview, parse_days

_DAYS_HELP = "Comma-separated days to process (default: Mon-Sat)"
_SCRCPY_HELP = "Skip launching scrcpy (use if already open)"
_HEADLESS_HELP = "Run browser without a visible window"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transfer Strivee CrossFit programming to BTWB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG-level logging")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("capture", help="Step 1 — capture phone screen via ADB")
    p.add_argument("--days", metavar="Mon,Tue,...", help=_DAYS_HELP)
    p.add_argument("--no-scrcpy", action="store_true", help=_SCRCPY_HELP)

    p = sub.add_parser("analyse", help="Step 2 — run vision analysis on saved captures")
    p.add_argument("--days", metavar="Mon,Tue,...", help=_DAYS_HELP)

    p = sub.add_parser("preview", help="Step 3 — show full block content as it would go to BTWB")
    p.add_argument("--days", metavar="Mon,Tue,...", help=_DAYS_HELP)

    p = sub.add_parser("post", help="Step 4 — post cached results to BTWB")
    p.add_argument("--days", metavar="Mon,Tue,...", help=_DAYS_HELP)
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    p.add_argument("--headless", action="store_true", help=_HEADLESS_HELP)

    p = sub.add_parser("run", help="Run all steps: capture -> analyse -> preview -> post")
    p.add_argument("--days", metavar="Mon,Tue,...", help=_DAYS_HELP)
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation before posting")
    p.add_argument("--headless", action="store_true", help=_HEADLESS_HELP)
    p.add_argument("--no-scrcpy", action="store_true", help=_SCRCPY_HELP)

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    log.setup(debug=args.debug)

    days = parse_days(args.days)
    no_scrcpy = getattr(args, "no_scrcpy", False)
    yes = getattr(args, "yes", False)
    headless = getattr(args, "headless", False)

    if args.command == "capture":
        do_capture(days, no_scrcpy)
    elif args.command == "analyse":
        do_analyse(days)
    elif args.command == "preview":
        do_preview(days)
    elif args.command == "post":
        do_post(days, yes, headless)
    elif args.command == "run":
        do_capture(days, no_scrcpy)
        do_analyse(days)
        do_preview(days)
        do_post(days, yes, headless)
