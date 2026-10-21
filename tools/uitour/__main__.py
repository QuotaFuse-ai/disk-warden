# -*- coding: utf-8 -*-
"""``python -m tools.uitour`` — one command, every page, every viewport.

Examples
--------
Run the whole matrix and open the gallery::

    python -m tools.uitour --out dist/uitour

Iterate on a single page while editing fixtures::

    python -m tools.uitour --only ide,rules --widths 1280 --themes light --langs zh-CN

In CI, additionally write the job summary::

    python -m tools.uitour --out dist/uitour --summary "$GITHUB_STEP_SUMMARY"

Exit code is 1 when a shot logged a console error or called an RPC method
without a fixture — both mean the screenshot is not trustworthy.
"""

from __future__ import annotations

import argparse
import sys

from . import gallery, tour


def _split(value, cast=str):
    return [cast(v) for v in str(value).replace(" ", "").split(",") if v]


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m tools.uitour",
        description="Screenshot ui/index.html across pages, widths, themes and languages.",
    )
    parser.add_argument(
        "--out", default="dist/uitour", help="output directory (default: dist/uitour)"
    )
    parser.add_argument(
        "--widths",
        default=",".join(str(w) for w in tour.WIDTHS),
        help="comma separated viewport widths (default: %(default)s)",
    )
    parser.add_argument(
        "--themes", default=",".join(tour.THEMES), help="light,dark (default: %(default)s)"
    )
    parser.add_argument(
        "--langs", default=",".join(tour.LANGS), help="zh-CN,en (default: %(default)s)"
    )
    parser.add_argument(
        "--only", default="", help="comma separated page names (see --list-pages)"
    )
    parser.add_argument(
        "--allow-console-errors",
        action="store_true",
        help="do not fail the run on console errors (fixture misses still fail)",
    )
    parser.add_argument(
        "--summary", default="", help="also write a markdown summary to this path"
    )
    parser.add_argument(
        "--no-gallery", action="store_true", help="skip writing index.html"
    )
    parser.add_argument(
        "--list-pages", action="store_true", help="print page names and exit"
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_pages:
        for spec in tour.PAGES:
            print("%-22s %-10s %s" % (spec["name"], spec["group"], spec["title"]))
        return 0

    only = set(_split(args.only)) if args.only else None
    if only:
        known = {spec["name"] for spec in tour.PAGES}
        unknown = sorted(only - known)
        if unknown:
            parser.error("unknown page(s): %s" % ", ".join(unknown))

    def echo(line):
        print(line, flush=True)

    echo("uitour: %d pages -> %s" % (len(only) if only else len(tour.PAGES), args.out))

    try:
        shots, problems = tour.run(
            args.out,
            widths=_split(args.widths, int),
            themes=_split(args.themes),
            langs=_split(args.langs),
            only=only,
            fail_on_console_error=not args.allow_console_errors,
            echo=echo,
        )
    except tour.TourError as exc:
        print("uitour: %s" % exc, file=sys.stderr)
        return 2

    if not args.no_gallery:
        target = gallery.write_gallery(args.out, shots)
        echo("gallery: %s" % target)
    gallery.write_manifest(args.out, shots, problems)

    if args.summary:
        picks = [
            ("welcome", 1280, "light", "zh-CN"),
            ("ide", 1280, "light", "zh-CN"),
            ("rules", 1280, "light", "zh-CN"),
            ("history", 1280, "light", "zh-CN"),
            ("migrate-progress", 1280, "light", "zh-CN"),
        ]
        text = gallery.summary_markdown(shots, args.out, picks)
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
        echo("summary: appended to %s" % args.summary)

    if problems:
        echo("")
        echo("uitour: %d problem(s)" % len(problems))
        for line in problems:
            echo("  - " + line)
        return 1

    echo("uitour: clean — %d shots, 0 console errors" % len(shots))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
