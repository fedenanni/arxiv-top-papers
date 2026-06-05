"""argparse subcommands: ingest / refresh / report."""

from __future__ import annotations

import argparse
import sys

from . import citations, db, dump, report, web


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--db",
        default=db.DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {db.DEFAULT_DB_PATH})",
    )
    p.add_argument(
        "--no-progress",
        action="store_true",
        help="disable progress bars",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csai-citations",
        description="Top-cited arXiv cs.AI papers per month.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # import-dump
    p_import = sub.add_parser(
        "import-dump",
        help="load paper metadata from the Kaggle arXiv snapshot (offline, no rate limit)",
    )
    p_import.add_argument(
        "--file",
        required=True,
        help="path to arxiv-metadata-oai-snapshot.json (JSON Lines)",
    )
    p_import.add_argument("--from", dest="date_from", help="start month YYYY-MM")
    p_import.add_argument("--to", dest="date_to", help="end month YYYY-MM (inclusive)")
    p_import.add_argument(
        "--category",
        nargs="+",
        dest="categories",
        default=dump.DEFAULT_CATEGORIES,
        metavar="CAT",
        help="arXiv categories to import (default: %(default)s)",
    )
    p_import.add_argument(
        "--match",
        choices=["primary", "any"],
        default="primary",
        help="category as primary only, or appearing anywhere (default: primary)",
    )
    _add_common(p_import)

    # refresh
    p_refresh = sub.add_parser("refresh", help="fetch/update citation counts")
    p_refresh.add_argument("--month", help="only papers in this month YYYY-MM")
    p_refresh.add_argument(
        "--stale-days",
        type=int,
        help="only papers fetched longer ago than N days (or never)",
    )
    p_refresh.add_argument(
        "--source",
        choices=[
            citations.SOURCE_S2,
            citations.SOURCE_OPENALEX,
            citations.SOURCE_HN,
        ],
        default=citations.SOURCE_S2,
        help="metric source: s2/openalex (citations) or hn (Hacker News, "
        "social) (default: s2)",
    )
    p_refresh.add_argument(
        "--only-missing",
        action="store_true",
        help="only papers with no / NULL count",
    )
    _add_common(p_refresh)

    # report
    p_report = sub.add_parser("report", help="rank and output top-k per month")
    grp = p_report.add_mutually_exclusive_group()
    grp.add_argument("--month", help="report a single month YYYY-MM")
    grp.add_argument(
        "--all-months", action="store_true", help="report every month"
    )
    p_report.add_argument("--top", type=int, default=10, help="top-k per month (default: 10)")
    p_report.add_argument(
        "--by",
        choices=[report.METRIC_CITATIONS, report.METRIC_SOCIAL],
        default=report.METRIC_CITATIONS,
        help="ranking metric: citations or social (Hacker News points) "
        "(default: citations)",
    )
    p_report.add_argument(
        "--format",
        choices=["table", "csv", "json"],
        default="table",
        help="output format (default: table)",
    )
    p_report.add_argument("--out", help="write to FILE instead of stdout")
    _add_common(p_report)

    # serve
    p_serve = sub.add_parser("serve", help="browse the database in a web UI")
    p_serve.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    _add_common(p_serve)

    return parser


def _cmd_import_dump(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        summary = dump.import_dump(
            conn,
            path=args.file,
            categories=args.categories,
            match=args.match,
            date_from=args.date_from,
            date_to=args.date_to,
            progress=not args.no_progress,
        )
    finally:
        conn.close()
    print(
        f"\nimport: scanned={summary.scanned} matched={summary.matched} "
        f"new={summary.new} updated={summary.updated}"
    )
    for month in sorted(summary.per_month):
        print(f"  {month}: {summary.per_month[month]}")
    return 0


def _cmd_refresh(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        summary = citations.refresh(
            conn,
            source=args.source,
            month=args.month,
            stale_days=args.stale_days,
            only_missing=args.only_missing,
            progress=not args.no_progress,
        )
    finally:
        conn.close()
    print(
        f"\nrefresh ({args.source}): selected={summary.selected} "
        f"fetched={summary.fetched} not_found={summary.not_found} "
        f"errors={summary.errors}"
    )
    for msg in summary.error_messages[:10]:
        print(f"  error: {msg}", file=sys.stderr)
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    month = None if args.all_months else args.month
    if month is None and not args.all_months:
        print(
            "report: specify --month YYYY-MM or --all-months", file=sys.stderr
        )
        return 2

    conn = db.connect(args.db)
    try:
        rows = report.rank_rows(conn, month=month, top=args.top, metric=args.by)
    finally:
        conn.close()

    output = report.render(rows, args.format, args.by)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(output)
            if not output.endswith("\n"):
                fh.write("\n")
        print(f"wrote {len(rows)} rows to {args.out}")
    else:
        print(output)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    web.serve(args.db, host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = {
        "import-dump": _cmd_import_dump,
        "refresh": _cmd_refresh,
        "report": _cmd_report,
        "serve": _cmd_serve,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
