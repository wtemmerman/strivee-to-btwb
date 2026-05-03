"""Command-line interface: argument parsing and subcommand dispatch."""

import argparse
from datetime import date

from .core import log
from .pipeline import do_analyse, do_capture, do_post, do_preview, parse_days, week_start

_DAYS_HELP = "Comma-separated days to process (default: Mon-Sat)"
_WEEK_HELP = "Week to process as YYYY-MM-DD (any day in the week); defaults to current week"


def _parse_week(raw: str | None) -> date | None:
    if raw is None:
        return None
    try:
        anchor = date.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date '{raw}' — expected YYYY-MM-DD")
    return week_start(anchor)


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
    p.add_argument("--week", metavar="YYYY-MM-DD", help=_WEEK_HELP)
    p.add_argument("--no-scrcpy", action="store_true", help="Skip launching scrcpy")

    p = sub.add_parser("analyse", help="Step 2 — run vision analysis on saved captures")
    p.add_argument("--days", metavar="Mon,Tue,...", help=_DAYS_HELP)
    p.add_argument("--week", metavar="YYYY-MM-DD", help=_WEEK_HELP)

    p = sub.add_parser("preview", help="Step 3 — show formatted block content before posting")
    p.add_argument("--days", metavar="Mon,Tue,...", help=_DAYS_HELP)
    p.add_argument("--week", metavar="YYYY-MM-DD", help=_WEEK_HELP)

    p = sub.add_parser("post", help="Step 4 — post cached results to BTWB")
    p.add_argument("--days", metavar="Mon,Tue,...", help=_DAYS_HELP)
    p.add_argument("--week", metavar="YYYY-MM-DD", help=_WEEK_HELP)
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    p.add_argument("--headless", action="store_true", help="Run browser without a visible window")

    p = sub.add_parser("run", help="Run all steps: capture → analyse → preview → post")
    p.add_argument("--days", metavar="Mon,Tue,...", help=_DAYS_HELP)
    p.add_argument("--week", metavar="YYYY-MM-DD", help=_WEEK_HELP)
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation before posting")
    p.add_argument("--headless", action="store_true", help="Run browser without a visible window")
    p.add_argument("--no-scrcpy", action="store_true", help="Skip launching scrcpy")

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    log.setup(debug=args.debug)

    days = parse_days(getattr(args, "days", None))
    ws = _parse_week(getattr(args, "week", None))

    if args.command == "capture":
        do_capture(days, getattr(args, "no_scrcpy", False), ws)
    elif args.command == "analyse":
        do_analyse(days, ws)
    elif args.command == "preview":
        do_preview(days, ws)
    elif args.command == "post":
        do_post(days, getattr(args, "yes", False), getattr(args, "headless", False), ws)
    elif args.command == "run":
        do_capture(days, getattr(args, "no_scrcpy", False), ws)
        do_analyse(days, ws)
        do_preview(days, ws)
        do_post(days, getattr(args, "yes", False), getattr(args, "headless", False), ws)
